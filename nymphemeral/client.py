import os
import re
import subprocess
import hashlib
import sys
import shutil
import threading
import Queue
import ConfigParser
import email
import itertools
from binascii import b2a_base64, a2b_base64

import gnupg
from passlib.utils.pbkdf2 import pbkdf2
from pyaxo import Axolotl

import aampy
import message
from errors import *
from nym import Nym

cfg = ConfigParser.ConfigParser()

BASE_FILES_PATH = '/usr/share/nymphemeral'
USER_PATH = os.path.expanduser('~')
NYMPHEMERAL_PATH = USER_PATH + '/.config/nymphemeral'
CONFIG_FILE = NYMPHEMERAL_PATH + '/nymphemeral.cfg'
OUTPUT_METHOD = {
    'mixmaster': 1,
    'sendmail': 2,
    'manual': 3,
}


def files_in_path(path):
    return [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]


def create_directory(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)


def create_dictionary(string):
    return dict(t.split() for t in string.strip().split('\n'))


def search_pgp_message(data):
    re_pgp = re.compile('-----BEGIN PGP MESSAGE-----.*-----END PGP MESSAGE-----', flags=re.DOTALL)
    return re_pgp.search(data)

def is_pgp_message(data):
    re_pgp = re.compile('-----BEGIN PGP MESSAGE-----.*-----END PGP MESSAGE-----$', flags=re.DOTALL)
    return re_pgp.match(data)


def read_data(identifier):
    try:
        with open(identifier, 'r') as f:
            return f.read()
    except IOError:
        print 'Error while reading ' + identifier
    return None


def save_data(data, identifier):
    try:
        with open(identifier, 'w') as f:
            f.write(data)
            return True
    except IOError:
        print 'Error while writing to ' + identifier
    return False


def new_gpg(paths):
    keyring = []
    for r in list(itertools.product(paths, ['/pubring.gpg'])):
        keyring.append(''.join(r))
    secret_keyring = []
    for r in list(itertools.product(paths, ['/secring.gpg'])):
        secret_keyring.append(''.join(r))
    binary = '/usr/bin/gpg'
    gpg = gnupg.GPG(binary,
                    paths[0],
                    keyring=keyring,
                    secret_keyring=secret_keyring,
                    options=['--personal-digest-preferences=sha256',
                             '--s2k-digest-algo=sha256'])
    gpg.encoding = 'latin-1'
    return gpg


def generate_key(gpg, name, address, passphrase, duration):
    input_data = gpg.gen_key_input(key_type='RSA', key_length='4096', subkey_type='RSA',
                                   subkey_length='4096', key_usage='sign,auth',
                                   subkey_usage='encrypt', expire_date=duration,
                                   passphrase=passphrase, name_real=name,
                                   name_comment='', name_email=address)
    fingerprint = gpg.gen_key(input_data).fingerprint
    return gpg.export_keys(keyids=address), fingerprint


def retrieve_fingerprint(gpg, address):
    keys = gpg.list_keys()
    for item in keys:
        if address in item['uids'][0]:
            return item['fingerprint']
    return None


def encrypt_data(gpg, data, recipients, fingerprint, passphrase):
    result = gpg.encrypt(data,
                         recipients,
                         sign=fingerprint,
                         passphrase=passphrase,
                         always_trust=True)
    if result.ok:
        return str(result)
    else:
        return None


def decrypt_data(gpg, data, passphrase):
    result = gpg.decrypt(data,
                         passphrase=passphrase,
                         always_trust=True)
    if result.ok:
        return str(result)
    else:
        return None


class Client:
    def __init__(self):
        self.directory_base = None
        self.directory_db = None
        self.directory_read_messages = None
        self.directory_unread_messages = None
        self.directory_gpg = None
        self.file_hsub = None
        self.file_encrypted_hsub = None
        self.is_debugging = None
        self.output_method = None
        self.file_mix_binary = None
        self.file_mix_cfg = None
        self.check_base_files()

        self.gpg = new_gpg([self.directory_base])

        self.axolotl = None
        self.nym = None
        self.hsubs = {}

        self.chain = self.retrieve_mix_chain()

    def debug(self, info):
        if self.is_debugging:
            print info

    def check_base_files(self):
        try:
            self.load_configs()
            create_directory(self.directory_db)
            shutil.copyfile(BASE_FILES_PATH + '/db/generic.db', self.directory_db + '/generic.db')
            create_directory(self.directory_read_messages)
            create_directory(self.directory_unread_messages)
        except IOError:
            print 'Error while creating the base files'
            raise

    def load_configs(self):
        try:
            # load default configs
            cfg.add_section('gpg')
            cfg.set('gpg', 'base_folder', USER_PATH + '/.gnupg')
            cfg.add_section('main')
            cfg.set('main', 'base_folder', NYMPHEMERAL_PATH)
            cfg.set('main', 'db_folder', '%(base_folder)s/db')
            cfg.set('main', 'messages_folder', '%(base_folder)s/messages')
            cfg.set('main', 'read_folder', '%(messages_folder)s/read')
            cfg.set('main', 'unread_folder', '%(messages_folder)s/unread')
            cfg.set('main', 'hsub_file', '%(base_folder)s/hsubs.txt')
            cfg.set('main', 'encrypted_hsub_file', '%(base_folder)s/encrypted_hsubs.txt')
            cfg.set('main', 'debug_switch', 'False')
            cfg.set('main', 'output_method', 'manual')
            cfg.add_section('mixmaster')
            cfg.set('mixmaster', 'base_folder', USER_PATH + '/Mix')
            cfg.set('mixmaster', 'binary', '%(base_folder)s/mixmaster')
            cfg.set('mixmaster', 'cfg', '%(base_folder)s/mix.cfg')
            cfg.add_section('newsgroup')
            cfg.set('newsgroup', 'base_folder', NYMPHEMERAL_PATH)
            cfg.set('newsgroup', 'group', 'alt.anonymous.messages')
            cfg.set('newsgroup', 'server', 'localhost')
            cfg.set('newsgroup', 'port', '119')
            cfg.set('newsgroup', 'newnews', '%(base_folder)s/.newnews')

            # parse existing configs in case new versions modify them
            # or the user modifies the file inappropriately
            if os.path.exists(CONFIG_FILE):
                saved_cfg = ConfigParser.ConfigParser()
                saved_cfg.read(CONFIG_FILE)
                for section in saved_cfg.sections():
                    try:
                        for option in cfg.options(section):
                            try:
                                cfg.set(section, option, saved_cfg.get(section, option))
                            except:
                                pass
                    except:
                        pass
            else:
                create_directory(NYMPHEMERAL_PATH)
            self.save_configs()

            self.directory_base = cfg.get('main', 'base_folder')
            self.directory_db = cfg.get('main', 'db_folder')
            self.directory_read_messages = cfg.get('main', 'read_folder')
            self.directory_unread_messages = cfg.get('main', 'unread_folder')
            self.directory_gpg = cfg.get('gpg', 'base_folder')
            self.file_hsub = cfg.get('main', 'hsub_file')
            self.file_encrypted_hsub = cfg.get('main', 'encrypted_hsub_file')
            self.is_debugging = cfg.getboolean('main', 'debug_switch')
            self.output_method = cfg.get('main', 'output_method')
            self.file_mix_binary = cfg.get('mixmaster', 'binary')
            self.file_mix_cfg = cfg.get('mixmaster', 'cfg')
        except IOError:
            print 'Error while opening ' + str(CONFIG_FILE).split('/')[-1]
            raise

    def save_configs(self):
        with open(CONFIG_FILE, 'w') as config_file:
            cfg.write(config_file)

    def update_configs(self):
        cfg.set('main', 'output_method', self.output_method)

    def retrieve_mix_chain(self):
        chain = None
        try:
            with open(self.file_mix_cfg, 'r') as config:
                lines = config.readlines()
                for line in lines:
                    s = re.search('(CHAIN )(.*)', line)
                    if s:
                        chain = 'Mix Chain: ' + s.group(2)
                        break
        except IOError:
            self.debug('Error while manipulating ' + self.file_mix_cfg.split('/')[-1])
        return chain

    def save_key(self, key, server=None):
        # also used to update an identity
        if server:
            self.gpg.delete_keys(self.retrieve_servers()[server])
        return self.gpg.import_keys(key)

    def delete_key(self, server):
        return self.gpg.delete_keys(self.retrieve_servers()[server])

    def retrieve_servers(self):
        servers = {}
        keys = self.gpg.list_keys()
        for item in keys:
            config_match = None
            send_match = None
            url_match = None
            for uid in item['uids']:
                if not config_match:
                    config_match = re.search('[^( |<)]*config@[^( |>)]*', uid)
                if not send_match:
                    send_match = re.search('[^( |<)]*send@[^( |>)]*', uid)
                if not url_match:
                    url_match = re.search('[^( |<)]*url@[^( |>)]*', uid)
            if config_match and send_match and url_match:
                server = config_match.group(0).split('@')[1]
                servers[server] = item['fingerprint']
        return servers

    def retrieve_nyms(self):
        nyms = []
        keys = self.gpg.list_keys()
        for item in keys:
            if len(item['uids']) is 1:
                search = re.search('(?<=<).*(?=>)', item['uids'][0])
                if search:
                    address = search.group()
                    nym = Nym(address,
                              fingerprint=item['fingerprint'])
                    nyms.append(nym)
        return nyms

    def start_session(self, nym, creating_nym=False):
        if nym.server not in self.retrieve_servers():
            raise NymservNotFoundError(nym.server)
        result = filter(lambda n: n.address == nym.address, self.retrieve_nyms())
        if not result:
            if not creating_nym:
                raise NymNotFoundError(nym.address)
        else:
            nym.fingerprint = result[0].fingerprint
            if not nym.fingerprint:
                raise FingerprintNotFoundError(nym.address)
            db_name = self.directory_db + '/' + nym.fingerprint + '.db'
            try:
                # workaround to suppress prints by pyaxo
                sys.stdout = open(os.devnull, 'w')
                self.axolotl = Axolotl(nym.fingerprint, db_name, nym.passphrase)
                sys.stdout = sys.__stdout__
            except SystemExit:
                sys.stdout = sys.__stdout__
                raise IncorrectPassphraseError
        self.nym = nym
        self.hsubs = self.retrieve_hsubs()
        if not creating_nym:
            self.nym.hsub = self.hsubs[nym.address]

    def end_session(self):
        self.axolotl = None
        self.nym = None
        self.hsubs = {}

    def decrypt_hsubs_file(self):
        if os.path.exists(self.file_encrypted_hsub):
            encrypted_data = read_data(self.file_encrypted_hsub)
            return decrypt_data(self.gpg, encrypted_data, self.nym.passphrase)
        else:
            self.debug('Decryption of ' + self.file_encrypted_hsub + ' failed. It does not exist')
        return None

    def save_hsubs(self, hsubs):
        output_file = self.file_hsub
        data = ''
        for key, item in hsubs.iteritems():
            data += key + ' ' + str(item) + '\n'
        # check if the nym has access or can create the encrypted hSub passphrases file
        if self.nym.fingerprint and (not os.path.exists(self.file_encrypted_hsub) or self.decrypt_hsubs_file()):
            nyms = self.retrieve_nyms()
            recipients = []
            for n in nyms:
                recipients.append(n.address)
            result = encrypt_data(self.gpg, data, recipients, self.nym.fingerprint, self.nym.passphrase)
            if result:
                output_file = self.file_encrypted_hsub
                data = result
        if save_data(data, output_file):
            if output_file == self.file_encrypted_hsub:
                if os.path.exists(self.file_hsub):
                    os.unlink(self.file_hsub)
                self.debug('The hsubs were encrypted and saved to ' + self.file_encrypted_hsub)
            return True
        else:
            return False

    def add_hsub(self, nym):
        self.hsubs[nym.address] = nym.hsub
        return self.save_hsubs(self.hsubs)

    def delete_hsub(self, nym):
        del self.hsubs[nym.address]
        # check if there are no hSub passphrases anymore
        if not self.hsubs or len(self.hsubs) == 1 and 'time' in self.hsubs:
            if self.decrypt_hsubs_file():
                hsub_file = self.file_encrypted_hsub
            else:
                hsub_file = self.file_hsub
            try:
                os.unlink(hsub_file)
            except IOError:
                print 'Error while manipulating ' + hsub_file.split('/')[-1]
                return False
        else:
            return self.save_hsubs(self.hsubs)
        return True

    def retrieve_hsubs(self):
        hsubs = {}
        encrypt_hsubs = False

        if os.path.exists(self.file_hsub):
            hsubs = create_dictionary(read_data(self.file_hsub))

        if os.path.exists(self.file_encrypted_hsub):
            decrypted_data = self.decrypt_hsubs_file()
            if decrypted_data:
                decrypted_hsubs = create_dictionary(decrypted_data)
                # check if there are unencrypted hSub passphrases
                if hsubs:
                    encrypt_hsubs = True
                    # merge hSub passphrases and save the "older" time to ensure messages are not skipped
                    try:
                        if hsubs['time'] < decrypted_hsubs['time']:
                            hsubs = dict(decrypted_hsubs.items() + hsubs.items())
                        else:
                            hsubs = dict(hsubs.items() + decrypted_hsubs.items())
                    except KeyError:
                         hsubs = dict(hsubs.items() + decrypted_hsubs.items())
                else:
                    hsubs = decrypted_hsubs
        else:
            encrypt_hsubs = True
        if hsubs and encrypt_hsubs:
            self.save_hsubs(hsubs)
        return hsubs

    def append_messages_to_list(self, read_messages, messages, messages_without_date):
        # check which folder to read the files from
        if read_messages:
            path = self.directory_read_messages
        else:
            path = self.directory_unread_messages
        files = files_in_path(path)
        for file_name in files:
            if re.match('message_' + self.nym.address + '_.*', file_name):
                file_path = path + '/' + file_name
                data = read_data(file_path)
                if read_messages:
                    if is_pgp_message(data):
                        decrypted_data = decrypt_data(self.gpg, data, self.nym.passphrase)
                        if decrypted_data:
                            data = decrypted_data
                    else:
                        encrypted_data = encrypt_data(self.gpg, data, self.nym.address, self.nym.fingerprint,
                                                      self.nym.passphrase)
                        if encrypted_data:
                            save_data(encrypted_data, file_path)
                            self.debug(file_path.split('/')[-1] + ' is now encrypted')
                new_message = message.Message(not read_messages, data, file_path)
                if new_message.date:
                    messages.append(new_message)
                else:
                    messages_without_date.append(new_message)

    def retrieve_messages_from_disk(self):
        messages = []
        messages_without_date = []
        self.append_messages_to_list(False, messages, messages_without_date)
        self.append_messages_to_list(True, messages, messages_without_date)
        messages = sorted(messages, key=lambda item: item.date, reverse=True)
        messages += messages_without_date
        return messages

    def send_create(self, ephemeral, hsub, name, duration):
        recipient = 'config@' + self.nym.server
        pubkey, fingerprint = generate_key(self.gpg, name, self.nym.address, self.nym.passphrase, duration)
        print fingerprint, pubkey
        self.generate_db(fingerprint, ephemeral, self.nym.passphrase)
        data = 'ephemeral: ' + ephemeral + '\nhsub: ' + hsub + '\n' + pubkey

        self.nym.fingerprint = fingerprint
        self.nym.hsub = hsub
        success, info, ciphertext = self.encrypt_and_send(data, recipient, self.nym)
        if success:
            db_name = self.directory_db + '/' + self.nym.fingerprint + '.db'
            self.axolotl = Axolotl(self.nym.fingerprint, db_name, self.nym.passphrase)
            self.add_hsub(self.nym)
        return success, info, ciphertext

    def send_message(self, nym, target_address, subject, content):
        recipient = 'send@' + nym.server
        msg = email.message_from_string('To: ' + target_address +
                                        '\nSubject: ' + subject +
                                        '\n' + content).as_string().strip()

        self.axolotl.loadState(nym.fingerprint, 'a')
        ciphertext = b2a_base64(self.axolotl.encrypt(msg)).strip()
        self.axolotl.saveState()

        lines = [ciphertext[i:i + 64] for i in xrange(0, len(ciphertext), 64)]
        pgp_message = '-----BEGIN PGP MESSAGE-----\n\n'
        for line in lines:
            pgp_message += line + '\n'
        pgp_message += '-----END PGP MESSAGE-----\n'

        return self.encrypt_and_send(pgp_message, recipient, nym)

    def send_config(self, nym, ephemeral, hsub, name):
        db_file = self.directory_db + '/' + nym.fingerprint + '.db'
        recipient = 'config@' + nym.server
        reset_db = False
        reset_hsub = False
        ephemeral_line = ''
        hsub_line = ''
        name_line = ''

        if ephemeral is not '':
            ephemeral_line = 'ephemeral: ' + ephemeral + '\n'
            reset_db = True
        if hsub is not '':
            hsub_line = 'hsub: ' + hsub + '\n'
            reset_hsub = True
        if name is not '':
            name_line = 'name: ' + name + '\n'

        success, info, ciphertext = None
        data = ephemeral_line + hsub_line + name_line
        if data is not '':
            success, info, ciphertext = self.encrypt_and_send(data, recipient, nym)
            if success:
                if reset_db:
                    if os.path.exists(db_file):
                        os.unlink(db_file)
                    self.generate_db(nym.fingerprint, ephemeral, nym.passphrase)
                if reset_hsub:
                    nym.hsub = hsub
                    self.add_hsub(nym)
        return success, info, ciphertext

    def send_delete(self, nym):
        recipient = 'config@' + self.nym.server
        db_file = self.directory_db + '/' + nym.fingerprint + '.db'

        data = 'delete: yes'
        success, info, ciphertext = self.encrypt_and_send(data, recipient, nym)
        if success:
            if os.path.exists(db_file):
                os.unlink(db_file)
            self.delete_hsub(nym.hsub)
            # delete secret key
            self.gpg.delete_keys(nym.fingerprint, True)
            # delete public key
            self.gpg.delete_keys(nym.fingerprint)
        return success, info, ciphertext

    def encrypt_and_send(self, data, recipient, nym):
        ciphertext = encrypt_data(self.gpg, data, recipient, nym.fingerprint, nym.passphrase)
        if ciphertext:
            success = True
            if self.output_method == 'manual':
                info = 'Send the following message to ' + recipient
            else:
                data = 'To: ' + recipient + '\nSubject: test\n\n' + ciphertext
                if self.send_data(data):
                    info = 'The following message was successfully sent to ' + recipient
                else:
                    info = 'ERROR! The following message could not be sent to ' + recipient
                    success = False
            info += '\n\n'
            return success, info, ciphertext
        else:
            raise IncorrectPassphraseError

    def send_data(self, data):
        if self.output_method == 'mixmaster':
            p = subprocess.Popen([self.file_mix_binary, '-m'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        elif self.output_method == 'sendmail':
            p = subprocess.Popen(['sendmail', '-t'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        else:
            self.debug('Invalid send choice')
            return False
        output, output_error = p.communicate(data)
        if output_error:
            return False
        if output or output == '':
            return True

    def count_unread_messages(self):
        counter = {}
        messages = files_in_path(self.directory_unread_messages)
        for m in messages:
            nym = re.search('(?<=message_).+(?=_.{5}.txt)', m)
            if nym:
                try:
                    counter[nym.group()] += 1
                except KeyError:
                    counter[nym.group()] = 1
        return counter

    def generate_db(self, fingerprint, mkey, passphrase):
        mkey = hashlib.sha256(mkey).digest()
        dbname = self.directory_db + '/generic.db'
        a = Axolotl('b', dbname, None)
        a.loadState('b', 'a')
        a.dbname = self.directory_db + '/' + fingerprint + '.db'
        a.dbpassphrase = passphrase
        if a.mode:  # alice mode
            RK = pbkdf2(mkey, b'\x00', 10, prf='hmac-sha256')
            HKs = pbkdf2(mkey, b'\x01', 10, prf='hmac-sha256')
            HKr = pbkdf2(mkey, b'\x02', 10, prf='hmac-sha256')
            NHKs = pbkdf2(mkey, b'\x03', 10, prf='hmac-sha256')
            NHKr = pbkdf2(mkey, b'\x04', 10, prf='hmac-sha256')
            CKs = pbkdf2(mkey, b'\x05', 10, prf='hmac-sha256')
            CKr = pbkdf2(mkey, b'\x06', 10, prf='hmac-sha256')
            CONVid = pbkdf2(mkey, b'\x07', 10, prf='hmac-sha256')
        else:  # bob mode
            RK = pbkdf2(mkey, b'\x00', 10, prf='hmac-sha256')
            HKs = pbkdf2(mkey, b'\x02', 10, prf='hmac-sha256')
            HKr = pbkdf2(mkey, b'\x01', 10, prf='hmac-sha256')
            NHKs = pbkdf2(mkey, b'\x04', 10, prf='hmac-sha256')
            NHKr = pbkdf2(mkey, b'\x03', 10, prf='hmac-sha256')
            CKs = pbkdf2(mkey, b'\x06', 10, prf='hmac-sha256')
            CKr = pbkdf2(mkey, b'\x05', 10, prf='hmac-sha256')
            CONVid = pbkdf2(mkey, b'\x07', 10, prf='hmac-sha256')

        a.state['RK'] = RK
        a.state['HKs'] = HKs
        a.state['HKr'] = HKr
        a.state['NHKs'] = NHKs
        a.state['NHKr'] = NHKr
        a.state['CKs'] = CKs
        a.state['CKr'] = CKr
        a.state['CONVid'] = CONVid
        a.state['name'] = fingerprint
        a.state['other_name'] = 'a'

        with a.db:
            cur = a.db.cursor()
            cur.execute('DELETE FROM conversations WHERE my_identity = "b"')
            a.saveState()

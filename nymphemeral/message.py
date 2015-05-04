import email
import re

from dateutil import parser


class Message(object):
    def __init__(self, is_unread, string, identifier):
        self._subject = None
        self._sender = None
        self._id = None
        self._date = None
        self._headers = None
        self._content = None
        self._title = None
        self._processed_message = None

        self.is_unread = is_unread
        self.processed_message = string
        self.identifier = identifier

    @property
    def subject(self):
        return self._subject

    @property
    def sender(self):
        return self._sender

    @property
    def id(self):
        return self._id

    @property
    def date(self):
        return self._date

    @property
    def headers(self):
        return self._headers

    @property
    def content(self):
        return self._content

    @property
    def title(self):
        return self._title

    @property
    def processed_message(self):
        return self._processed_message

    @processed_message.setter
    def processed_message(self, string):
        message = email.message_from_string(string)
        title = ''

        self._id = message.get('Message-ID')

        if 'Date' in message:
            self._date = parser.parse(message.get('Date'))
            title += str(self.date)[:16] + ' '

        if 'From' in message:
            sender = message.get('From')
            self._sender = re.search('[^( |<)]+@[^( |>)]+', sender).group(0)
            title += self.sender + ': '
        else:
            title = 'Unknown sender: '

        if 'Subject' in message:
            self._subject = message.get('Subject')
            title += self.subject
        else:
            title += '(no subject)'

        headers = []
        for item in message.items():
            headers.append(': '.join(item))
        self._headers = '\n'.join(headers)

        # content types we print
        mtypes = ('text/plain', 'text/html', 'message/rfc822')

        if message.is_multipart():
            content = ''
            for part in message.walk():
                if part['Content-Transfer-Encoding'] == 'base64' and part.get_content_type() in mtypes:
                    content += part.get_payload(decode=True)
                elif part.get_content_type() in mtypes:
                    content += part.as_string()
        else:
            if message['Content-Transfer-Encoding'] == 'base64':
                content = message.get_payload(decode=True)
            else:
                content = message.get_payload()
        self._content = content

        if self.is_unread:
            title = 'Undecrypted message'
            if self.date:
                title += ' - ' + str(self.date)

        self._title = title
        self._processed_message = message

from __future__ import unicode_literals

import json
import requests


class MageError(Exception):
    def __init__(self, caused_by=None, error_code=None):
        self.caused_by = caused_by
        self.error_code = error_code

    def __unicode__(self):
        return 'Caused by %s' % unicode(self.caused_by) if self.caused_by else 'Status code %d' % self.error_code

    def __str__(self):
        return str(unicode(self))


class MageClient(object):
    """
    Simple client for API calls to a Message Mage instance
    """
    def __init__(self, base_url, auth_token):
        self.base_url = base_url
        self.auth_token = auth_token
        self.client = requests.Session()

    def get_twitter_streams(self):
        return self._request('GET', 'twitter', {})

    def get_twitter_stream(self, channel_uuid):
        return self._request('GET', 'twitter/%s' % channel_uuid)

    def add_twitter_stream(self, channel_uuid):
        return self._request('POST', 'twitter', {'uuid': channel_uuid})

    def remove_twitter_stream(self, channel_uuid):
        return self._request('DELETE', 'twitter/%s' % channel_uuid)

    def _request(self, method, endpoint, params=None):
        url = self.base_url
        if not url.endswith('/'):
            url += '/'
        url += endpoint

        method = method.lower()
        func = getattr(self.client, method)
        params = params or {}

        requests_args = {
            'headers': {'Authorization': 'Token %s' % self.auth_token}
        }

        if method == 'get':
            requests_args['params'] = params
        else:
            requests_args['data'] = params

        try:
            response = func(url, **requests_args)
        except requests.RequestException as e:
            raise MageError(e)

        if response.status_code > 300:
            raise MageError(error_code=response.status_code)

        if response.content:
            return json.loads(response.content.decode('utf-8'))
        else:
            return ''


def mage_handle_new_message(org, msg):
    """
    Messages created Mage are only saved to the database. Here we take care of the other stuff
    """
    # update cached message count
    from temba.orgs.models import OrgEvent
    org.update_caches(OrgEvent.msg_new_incoming, msg)

    # Mage no longer assigns topups
    if not msg.topup_id:
        msg.topup_id = org.decrement_credit()
        msg.save(update_fields=('topup_id',))


def mage_handle_new_contact(org, contact):
    """
    Contacts created Mage are only saved to the database. Here we take care of the other stuff
    """
    # update cached contact count
    from temba.orgs.models import OrgEvent
    org.update_caches(OrgEvent.contact_new, contact)

    # possible to have dynamic groups based on name
    contact.handle_update(attrs=('name',))

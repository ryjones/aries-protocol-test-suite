"""Connection protocol messages and helpers."""

import base64
import json
import re
import uuid
from collections import namedtuple

from voluptuous import Schema, Optional
from aries_staticagent import Message, crypto
from ..schema import MessageSchema


TheirInfo = namedtuple(
    'TheirInfo',
    'endpoint, recipients, routing_keys'
)


class DIDDoc(dict):
    """DIDDoc class for creating and verifying DID Docs."""
    # DIDDoc specification is very flexible: https://w3c-ccg.github.io
    # This particular schema covers Ed25519 keys. All key types here: https://w3c-ccg.github.io/ld-cryptosuite-registry/
    VALIDATOR = Schema({
        "@context": "https://w3id.org/did/v1",
        "id": str,
        Optional("publicKey"): [{
            "id": str,
            "type": "Ed25519VerificationKey2018",
            "controller": str,
            "publicKeyBase58": str
        }],
        Optional("authentication"): [{ # This is not fully correct: https://w3c.github.io/did-core/#authentication
            "type": "Ed25519SignatureAuthentication2018",
            "publicKey": str
        }],
        "service": [{
            "id": str,
            "type": str,
            Optional("priority"): int,
            Optional("recipientKeys"): [str],
            Optional("routingKeys"): [str],
            "serviceEndpoint": str,
        }],
    })

    def validate(self):
        """Validate this DIDDoc."""
        DIDDoc.VALIDATOR(self)

    @classmethod
    def make(cls, my_did, my_vk, endpoint):
        """Make a DIDDoc dictionary."""
        return cls({
            "@context": "https://w3id.org/did/v1",
            "id": my_did,
            "publicKey": [{
                "id": my_did + "#keys-1",
                "type": "Ed25519VerificationKey2018",
                "controller": my_did,
                "publicKeyBase58": my_vk
            }],
            "service": [{
                "id": my_did + ";indy",
                "type": "IndyAgent",
                "recipientKeys": [my_vk],
                "routingKeys": [],
                "serviceEndpoint": endpoint,
            }],
        })


    @classmethod
    def parse_key_reference(cls, key: str):
        parts = key.split("#")
        return parts[1] or parts[0]


    def key_for_reference(self, key: str) -> Optional(str):
        key = self.parse_key_reference(key)
        return next((public_key['publicKeyBase58'] for public_key in self['publicKey']
                     if public_key['id'] == key or public_key['publicKeyBase58'] == key), None)


    def get_connection_info(self):
        """Extract connection information from DID Doc."""
        return TheirInfo(
            # self['publicKey'][0]['controller'],  # did
            self['service'][0]['serviceEndpoint'],  # endpoint
            [self.key_for_reference(recipient_key) for recipient_key in self['service'][0]['recipientKeys']],  # recipients
            [self.key_for_reference(routing_key) for routing_key in self['service'][0]['routingKeys']],  # routing (optional)
        )


class Invite(Message):
    """Invite Message"""
    TYPE = 'did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/connections/1.0/invitation'
    VALIDATOR = MessageSchema({
        '@type': TYPE,
        '@id': str,
        'label': str,
        'recipientKeys': [str],
        Optional('routingKeys'): [str],
        'serviceEndpoint': str,
    })

    def validate(self):
        """Validate this invite."""
        Invite.VALIDATOR(self)

    @classmethod
    def make(cls, label, key, endpoint):
        """Create a new Invite message."""
        return cls({
            '@type': Invite.TYPE,
            'label': label,
            'recipientKeys': [key],
            'serviceEndpoint': endpoint,
            'routingKeys': []
        })

    def to_url(self):
        """Create invite url from message."""
        b64_invite = base64.urlsafe_b64encode(
            bytes(self.serialize(), 'utf-8')
        ).decode('ascii')

        return '{}?c_i={}'.format(self['serviceEndpoint'], b64_invite)

    @classmethod
    def parse_invite(cls, invite: str):
        """Parse an invite url, returning a new message."""

        try:
            # If the invite is JSON already
            json.loads(invite)
        except ValueError:
            # If the invite is base64 url
            matches = re.match('(.+)?c_i=(.+)', invite)
            assert matches, 'Improperly formatted invite url!'
            invite = base64.urlsafe_b64decode(matches.group(2)).decode('ascii')

        invite_msg = cls.deserialize(invite)

        Invite.validate(invite_msg)

        return invite_msg

    def get_connection_info(self):
        """Get connection information out of invite message."""
        return TheirInfo(
            # None, #did
            self['serviceEndpoint'],
            self['recipientKeys'],
            self.get('routingKeys'),
        )


class Request(Message):
    """Request Message"""

    TYPE = 'did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/connections/1.0/request'
    VALIDATOR = MessageSchema({
        '@type': TYPE,
        '@id': str,
        'label': str,
        'connection': {
            'DID': str,
            'DIDDoc': DIDDoc.VALIDATOR
        }
    })

    def validate(self):
        """Validate this Request Message."""
        Request.VALIDATOR(self)

    @classmethod
    def make(cls, label, my_did, my_vk, endpoint):
        """Create a Request Message."""
        return cls({
            '@type': Request.TYPE,
            '@id': str(uuid.uuid4()),
            'label': label,
            'connection': {
                'DID': my_did,
                'DIDDoc': DIDDoc.make(my_did, my_vk, endpoint)
            }
        })

    def get_connection_info(self):
        """Get connection information out of Request Message."""
        return DIDDoc(self['connection']['DIDDoc']).get_connection_info()


class Response(Message):
    """Response Message"""
    TYPE = 'did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/connections/1.0/response'
    PRE_SIG_VERIFY_VALIDATOR = MessageSchema({
        '@type': TYPE,
        '@id': str,
        '~thread': {
            'thid': str,
            Optional('sender_order'): int
        },
        'connection~sig': object
    })
    POST_SIG_VERIFY_VALIDATOR = MessageSchema({
        '@type': TYPE,
        '@id': str,
        '~thread': {
            'thid': str,
            Optional('sender_order'): int
        },
        'connection': {
            'DID': str,
            'DIDDoc': DIDDoc.VALIDATOR
        }
    })

    def validate_pre_sig_verify(self):
        """Validate this response against pre sig verify schema."""
        Response.PRE_SIG_VERIFY_VALIDATOR(self)

    def validate_post_sig_verify(self):
        """Validate this response againts post sig verify schema."""
        Response.POST_SIG_VERIFY_VALIDATOR(self)

    @classmethod
    def make(cls, request_id, my_did, my_vk, endpoint):
        """Create new Response Message."""
        return cls({
            '@type': Response.TYPE,
            '@id': str(uuid.uuid4()),
            '~thread': {
                'thid': request_id,
                'sender_order': 0
            },
            'connection': {
                'DID': my_did,
                'DIDDoc': {
                    "@context": "https://w3id.org/did/v1",
                    "id": my_did,
                    "publicKey": [{
                        "id": my_did + "#keys-1",
                        "type": "Ed25519VerificationKey2018",
                        "controller": my_did,
                        "publicKeyBase58": my_vk
                    }],
                    "service": [{
                        "id": my_did + ";indy",
                        "type": "IndyAgent",
                        "priority": 0,
                        "recipientKeys": [my_vk],
                        "routingKeys": [],
                        "serviceEndpoint": endpoint,
                    }],
                }
            }
        })

    def sign(self, signer: str, secret: bytes):
        """Sign this response message."""
        self['connection~sig'] = crypto.sign_message_field(
            self['connection'],
            signer=signer,
            secret=secret
        )
        del self['connection']

    def verify_sig(self, expected_signer: str):
        """Verify signature on this response message."""
        signer, self['connection'] = \
            crypto.verify_signed_message_field(self['connection~sig'])
        assert signer == expected_signer, 'Unexpected signer'
        del self['connection~sig']

    def get_connection_info(self):
        """Get connection information out of Request Message."""
        return DIDDoc(self['connection']['DIDDoc']).get_connection_info()

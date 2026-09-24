"""Verify the edge signed-response envelope matches its canonical payload."""

import base64
import json

from tmads_edge_distributor.main import create_app, signed_object
from tmads_shared.crypto import verify_ed25519

app = create_app()
response = signed_object(
    app,
    {
        "schema": "tmads.signature-self-test.v1",
        "device_id": "test-device",
        "sequence": 123,
    },
)
payload = base64.urlsafe_b64decode(
    response["_signed_payload"] + "=" * (-len(response["_signed_payload"]) % 4)
)
decoded = json.loads(payload)
assert decoded["schema"] == "tmads.signature-self-test.v1"
assert decoded["signer_key_id"] == response["signer_key_id"]
assert verify_ed25519(decoded, response["signature"], app.state.signing_key.public_key())
print(
    json.dumps(
        {
            "verified": True,
            "signer_key_id": response["signer_key_id"],
            "payload_bytes": len(payload),
        }
    )
)

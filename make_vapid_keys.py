"""Run once:  python make_vapid_keys.py   -> prints the two keys to put in your environment variables."""
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
private = b64(key.private_numbers().private_value.to_bytes(32, "big"))
public = b64(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
print("VAPID_PUBLIC_KEY=" + public)
print("VAPID_PRIVATE_KEY=" + private)
print("VAPID_SUBJECT=mailto:you@example.com   (use your real e-mail)")
print("\nKeep the PRIVATE key secret. Do not change the keys later, or every device must re-subscribe.")

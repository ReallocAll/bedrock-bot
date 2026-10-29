import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from importlib.resources import files
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils


@dataclass(slots=True)
class Identity:
    name: str
    uuid: str
    key: ec.EllipticCurvePrivateKey = field(repr=False)
    xuid: str = ""
    token: str = field(init=False, repr=False)

    def __post_init__(self):
        self.token = _identity_token(self)


def create_identity(name: str, uuid: str, *, xuid: str = "") -> Identity:
    return Identity(name, uuid, ec.generate_private_key(ec.SECP384R1()), xuid=xuid)


def _base(data: bytes, url: bool = False) -> str:
    return (base64.urlsafe_b64encode(data).rstrip(b"=") if url else base64.b64encode(data)).decode("ascii")


def _decode(text: str, limit: int, url: bool = False) -> bytes:
    alphabet = r"[A-Za-z0-9_-]*={0,2}" if url else r"[A-Za-z0-9+/]*={0,2}"
    if (
        not isinstance(text, str)
        or len(text) > 4 * ((limit + 2) // 3)
        or len(text) % 4 == 1
        or not re.fullmatch(alphabet, text)
    ):
        raise ValueError("invalid or oversized base64 value")
    if "=" in text and (len(text) % 4 or len(text.rstrip("=")) % 4 < 2):
        raise ValueError("invalid base64 padding")
    result = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_" if url else None, validate=True)
    if len(result) > limit:
        raise ValueError("decoded base64 exceeds limit")
    return result


def _json(data: bytes) -> dict:
    depth = 0
    quoted = escaped = False
    for c in data:
        if quoted:
            if escaped:
                escaped = False
            elif c == 92:
                escaped = True
            elif c == 34:
                quoted = False
        elif c == 34:
            quoted = True
        elif c in (91, 123):
            depth += 1
            if depth > 16:
                raise ValueError("handshake JSON nesting exceeds limit")
        elif c in (93, 125):
            depth -= 1
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("invalid handshake JSON object")
    return result


def _sign(key, header: dict, body: dict) -> str:
    raw = ".".join(_base(json.dumps(value, separators=(",", ":")).encode(), True) for value in (header, body))
    r, s = utils.decode_dss_signature(key.sign(raw.encode(), ec.ECDSA(hashes.SHA384())))
    return raw + "." + _base(r.to_bytes(48, "big") + s.to_bytes(48, "big"), True)


def _header(key) -> dict:
    public = _base(
        key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return {"alg": "ES384", "x5u": public}


def _identity_token(identity: Identity) -> str:
    header = _header(identity.key)
    now = int(time.time())
    claims = dict(
        nbf=now - 21600,
        exp=now + 21600,
        aud=["api://auth-minecraft-services/multiplayer"],
        ipt="",
        mid="",
        tid="",
        cpk=header["x5u"],
        xid=identity.xuid,
        xname=identity.name,
        leguuid=identity.uuid,
    )
    token = _sign(identity.key, header, claims)
    if len(token.encode("ascii")) > 16384:
        raise ValueError("identity JWT exceeds 16384 bytes")
    return token


def build_login(identity: Identity, options) -> bytes:
    header = _header(identity.key)
    random = secrets.token_bytes(32)
    client = dict(
        AnimatedImageData=[],
        CapeData="",
        CapeId="",
        CapeImageHeight=0,
        CapeImageWidth=0,
        CapeOnClassicSkin=False,
        ClientRandomId=int.from_bytes(random[:8], "little") & ((1 << 63) - 1),
        CurrentInputMode=1,
        DefaultInputMode=1,
        DeviceModel="",
        DeviceOS=1,
        DeviceId=random[8:24].hex(),
        GameVersion="1.26.50" if options.protocol == 2193 else "1.26.30",
        GuiScale=0,
        FilterProfanity=False,
        ClientEditorConnectionIntent=0,
        ClientIsEditorCapable=False,
        LanguageCode="en_GB",
        PersonaSkin=False,
        PlatformOfflineId="",
        PlatformOnlineId="",
        PremiumSkin=False,
        SelfSignedId=identity.uuid,
        ServerAddress=(urlsplit(options.server_url).netloc if options.transport == "raknet" else options.server_url),
        SkinAnimationData="",
        SkinData=_base(b"\0\0\0\xff" * (64 * 32)),
        SkinGeometryData=_base(files("endpilot").joinpath("resources/skin_geometry.json").read_bytes()),
        SkinGeometryDataEngineVersion=_base(b"0.0.0"),
        SkinId=str(uuid4()),
        PlayFabId=random[24:].hex(),
        SkinImageHeight=32,
        SkinImageWidth=64,
        SkinResourcePatch=_base(files("endpilot").joinpath("resources/skin_resource_patch.json").read_bytes()),
        SkinColor="",
        ArmSize="",
        PersonaPieces=[],
        PieceTintColors=[],
        ThirdPartyName=identity.name,
        UIProfile=0,
        TrustedSkin=False,
        OverrideSkin=False,
        CompatibleWithClientSideChunkGen=options.client_side_generation,
        MaxViewDistance=96,
        MemoryTier=4 if options.client_side_generation else 0,
        PlatformType=0,
        GraphicsMode=0,
        PartyId="",
        IsPartyLeader=False,
        ProfileHash="",
    )
    envelope = json.dumps(
        {"Certificate": '{"chain":[""]}', "AuthenticationType": 2, "Token": identity.token},
        separators=(",", ":"),
    ).encode()
    token = _sign(identity.key, header, client).encode()
    return struct.pack("<I", len(envelope)) + envelope + struct.pack("<I", len(token)) + token


def derive_session_key(identity: Identity, jwt: str, *, server_key_sha256: str = "") -> bytes:
    try:
        if not isinstance(jwt, str) or len(jwt) > 65536:
            raise ValueError("server handshake JWT exceeds 64 KiB")
        if not isinstance(server_key_sha256, str) or (
            server_key_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", server_key_sha256)
        ):
            raise ValueError("invalid server key pin")
        parts = jwt.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError("invalid server handshake JWT structure")
        header = _json(_decode(parts[0], 65536, True))
        if header.get("alg") != "ES384" or not isinstance(header.get("x5u"), str):
            raise ValueError("server handshake requires ES384 and x5u")
        spki = _decode(header["x5u"], 2048)
        server = serialization.load_der_public_key(spki)
        if not isinstance(server, ec.EllipticCurvePublicKey) or not isinstance(server.curve, ec.SECP384R1):
            raise ValueError("server handshake key must be a P-384 SPKI")
        if server.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != spki:
            raise ValueError("invalid server handshake SPKI")
        if server_key_sha256 and not hmac.compare_digest(hashlib.sha256(spki).hexdigest(), server_key_sha256.lower()):
            raise ValueError("server handshake key pin mismatch")
        signature = _decode(parts[2], 96, True)
        if len(signature) != 96:
            raise ValueError("invalid server handshake signature length")
        signature = utils.encode_dss_signature(
            int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
        )
        server.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"), ec.ECDSA(hashes.SHA384()))
        body = _json(_decode(parts[1], 65536, True))
        salt = _decode(body["salt"], 1024)
        if not isinstance(identity.key.curve, ec.SECP384R1):
            raise ValueError("identity key must be P-384")
        return hashlib.sha256(salt + identity.key.exchange(ec.ECDH(), server)).digest()
    except (InvalidSignature, KeyError, TypeError, UnicodeError, ValueError):
        raise ValueError("invalid server handshake") from None

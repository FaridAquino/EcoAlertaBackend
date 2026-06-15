"""Lambdas de usuarios: register y login.

Almacena en la tabla usuariosEcoAlerta: correo, contrasena hasheada,
ubicacion (latitud/longitud como Decimal para DynamoDB) y las rutas escogidas.
"""
import json
import os
import hashlib
import hmac
import secrets
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["USUARIOS_TABLE"])
ruta_table = dynamodb.Table(os.environ["RUTA_TABLE"])

PBKDF2_ITERATIONS = 100_000


# ---------------------------------------------------------------------------
# Helpers comunes
# ---------------------------------------------------------------------------
class DecimalEncoder(json.JSONEncoder):
    """Serializa los Decimal de DynamoDB a numeros JSON."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            # int si es entero, float en otro caso
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def _parse_body(event):
    """Parsea el body usando Decimal para no perder precision en lat/lng."""
    raw = event.get("body") or "{}"
    return json.loads(raw, parse_float=Decimal)


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password, stored):
    try:
        salt_hex, hash_hex = stored.split("$")
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
def register(event, context):
    body = _parse_body(event)

    correo = body.get("correo")
    password = body.get("password")
    if not correo or not password:
        return _response(400, {"error": "correo y password son requeridos"})

    # lat/lng llegan como Decimal gracias a parse_float -> seguros para DynamoDB
    item = {
        "correo": correo,
        "password_hash": hash_password(password),
        "ubicacion": {
            "lat": body.get("latitud"),
            "lng": body.get("longitud"),
        },
        "rutas": body.get("rutas", []),
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(correo)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"error": "el correo ya esta registrado"})
        raise

    return _response(201, {"mensaje": "usuario registrado", "correo": correo})


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
def login(event, context):
    body = _parse_body(event)

    correo = body.get("correo")
    password = body.get("password")
    if not correo or not password:
        return _response(400, {"error": "correo y password son requeridos"})

    result = table.get_item(Key={"correo": correo})
    item = result.get("Item")
    if not item or not verify_password(password, item.get("password_hash", "")):
        return _response(401, {"error": "credenciales invalidas"})

    rutas_ids = item.get("rutas", [])

    return _response(
        200,
        {
            "mensaje": "login correcto",
            "correo": item["correo"],
            "ubicacion": item.get("ubicacion"),
            "rutas": rutas_ids,                       # IDs escogidos por el usuario
            "rutas_detalle": _detalle_rutas(rutas_ids),  # objetos completos (nodes, fechas, ...)
        },
    )


def _detalle_rutas(route_ids):
    """Lee rutaEcoAlerta y devuelve el detalle completo de las rutas escogidas.

    Usa BatchGetItem (una sola llamada) para Calendario (fechas), Rutas (nodes)
    y Seguimiento (route_id). Si una ruta ya no existe, simplemente se omite.
    """
    if not route_ids:
        return []

    keys = [{"route_id": rid} for rid in route_ids]
    resp = dynamodb.batch_get_item(
        RequestItems={ruta_table.name: {"Keys": keys}}
    )
    return resp.get("Responses", {}).get(ruta_table.name, [])

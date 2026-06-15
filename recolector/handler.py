"""Lambdas del recolector: register, login, registrarRuta e iniciarRuta.

- recolectorEcoAlerta: correo, contrasena hasheada, rutas, ruta_activa.
- rutaEcoAlerta: definicion de cada ruta (route_id, start, end, nodes, fechas).
"""
import json
import os
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
recolector_table = dynamodb.Table(os.environ["RECOLECTOR_TABLE"])
ruta_table = dynamodb.Table(os.environ["RUTA_TABLE"])

PBKDF2_ITERATIONS = 100_000

# Codigos de dia de la semana usados en el campo "fechas" de las rutas.
# weekday(): 0=Lunes ... 6=Domingo
DIA_CODIGO = {0: "L", 1: "M", 2: "MM", 3: "J", 4: "V", 5: "S", 6: "D"}

# Zona horaria de Peru (UTC-5, sin horario de verano).
PERU_TZ = timezone(timedelta(hours=-5))


# ---------------------------------------------------------------------------
# Helpers comunes
# ---------------------------------------------------------------------------
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
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
# register / login del recolector
# ---------------------------------------------------------------------------
def register(event, context):
    body = _parse_body(event)

    correo = body.get("correo")
    password = body.get("password")
    if not correo or not password:
        return _response(400, {"error": "correo y password son requeridos"})

    item = {
        "correo": correo,
        "password_hash": hash_password(password),
        "rutas": body.get("rutas", []),
    }

    try:
        recolector_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(correo)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(409, {"error": "el correo ya esta registrado"})
        raise

    return _response(201, {"mensaje": "recolector registrado", "correo": correo})


def login(event, context):
    body = _parse_body(event)

    correo = body.get("correo")
    password = body.get("password")
    if not correo or not password:
        return _response(400, {"error": "correo y password son requeridos"})

    result = recolector_table.get_item(Key={"correo": correo})
    item = result.get("Item")
    if not item or not verify_password(password, item.get("password_hash", "")):
        return _response(401, {"error": "credenciales invalidas"})

    return _response(
        200,
        {
            "mensaje": "login correcto",
            "correo": item["correo"],
            "rutas": item.get("rutas", []),
            "ruta_activa": item.get("ruta_activa"),
        },
    )


# ---------------------------------------------------------------------------
# registrarRuta -> rutaEcoAlerta
# ---------------------------------------------------------------------------
def registrarRuta(event, context):
    body = _parse_body(event)

    route_id = body.get("route_id")
    if not route_id:
        return _response(400, {"error": "route_id es requerido"})

    # nodes ya traen lat/lng como Decimal por parse_float -> seguros para DynamoDB
    item = {
        "route_id": route_id,
        "start": body.get("start"),
        "end": body.get("end"),
        "nodes": body.get("nodes", []),
        "fechas": body.get("fechas", []),
    }
    ruta_table.put_item(Item=item)

    return _response(201, {"mensaje": "ruta registrada", "route_id": route_id})


# ---------------------------------------------------------------------------
# iniciarRuta: valida que hoy este dentro de "fechas" y marca ruta_activa
# ---------------------------------------------------------------------------
def iniciarRuta(event, context):
    body = _parse_body(event)

    correo = body.get("correo")
    route_id = body.get("route_id")
    if not correo or not route_id:
        return _response(400, {"error": "correo y route_id son requeridos"})

    ruta = ruta_table.get_item(Key={"route_id": route_id}).get("Item")
    if not ruta:
        return _response(404, {"error": "la ruta no existe"})

    # Dia actual segun la hora local de Peru (UTC-5)
    ahora_peru = datetime.now(PERU_TZ)
    hoy = DIA_CODIGO[ahora_peru.weekday()]
    fechas = ruta.get("fechas", [])
    if hoy not in fechas:
        return _response(
            400,
            {
                "error": "ruta no disponible hoy",
                "dia_actual": hoy,
                "dias_validos": fechas,
            },
        )

    # El recolector debe existir para iniciar ruta
    try:
        recolector_table.update_item(
            Key={"correo": correo},
            UpdateExpression="SET ruta_activa = :r, iniciada_en = :t",
            ConditionExpression="attribute_exists(correo)",
            ExpressionAttributeValues={
                ":r": route_id,
                ":t": ahora_peru.isoformat(),  # ISO-8601 con offset -05:00 (Peru)
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _response(404, {"error": "recolector no encontrado"})
        raise

    return _response(
        200, {"mensaje": "ruta iniciada", "correo": correo, "route_id": route_id}
    )

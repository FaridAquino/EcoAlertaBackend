"""Lambdas de WebSocket: connect, disconnect, enviarUbicacion y enviarAlerta.

Tabla conexionesEcoAlerta: connection_id (PK), user_id, rol, route_id, ubicacion.
El route_id esta indexado (GSI route_id-index) para buscar las conexiones de una ruta.

enviarUbicacion: difunde la ubicacion del recolector a todos los conectados de su ruta y,
usando la MapBox Matrix API, a los que esten a <= 3 min les envia ademas el action enviarAlerta.
"""
import json
import os
import urllib.parse
import urllib.request
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["CONEXIONES_TABLE"])
ROUTE_INDEX = os.environ["ROUTE_INDEX"]
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "")

# Umbral de proximidad para la alerta: 3 minutos.
ALERTA_SEGUNDOS = 180


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _ok(body=None):
    return {"statusCode": 200, "body": json.dumps(body or {}, cls=DecimalEncoder)}


def _api_client(event):
    domain = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    return boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=f"https://{domain}/{stage}",
    )


def _qs(event):
    return event.get("queryStringParameters") or {}


def _to_decimal(value):
    """Convierte lat/lng a Decimal de forma segura para DynamoDB."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _post(client, connection_id, payload):
    """Envia un mensaje a una conexion; limpia conexiones muertas."""
    try:
        client.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(payload, cls=DecimalEncoder).encode(),
        )
        return True
    except client.exceptions.GoneException:
        table.delete_item(Key={"connection_id": connection_id})
        return False
    except ClientError:
        return False


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------
def connect(event, context):
    connection_id = event["requestContext"]["connectionId"]
    qs = _qs(event)

    item = {
        "connection_id": connection_id,
        "user_id": qs.get("user_id"),
        "rol": qs.get("rol"),          # "usuario" o "recolector"
        "route_id": qs.get("route_id"),
    }
    lat = _to_decimal(qs.get("lat"))
    lng = _to_decimal(qs.get("lng"))
    if lat is not None and lng is not None:
        item["ubicacion"] = {"lat": lat, "lng": lng}

    # No guardamos claves vacias (route_id no puede ser None para el GSI util)
    item = {k: v for k, v in item.items() if v is not None}
    table.put_item(Item=item)
    return _ok({"mensaje": "conectado"})


def disconnect(event, context):
    connection_id = event["requestContext"]["connectionId"]
    table.delete_item(Key={"connection_id": connection_id})
    return _ok({"mensaje": "desconectado"})


# ---------------------------------------------------------------------------
# enviarUbicacion
# ---------------------------------------------------------------------------
def enviarUbicacion(event, context):
    body = json.loads(event.get("body") or "{}", parse_float=Decimal)
    route_id = body.get("route_id")
    ubicacion = body.get("ubicacion") or {}
    if not route_id:
        return _ok({"error": "route_id es requerido"})

    rec_lat = ubicacion.get("lat")
    rec_lng = ubicacion.get("lng")

    client = _api_client(event)
    sender_id = event["requestContext"]["connectionId"]

    # Todas las conexiones de la ruta (GSI route_id-index)
    conexiones = _conexiones_de_ruta(route_id)

    # Solo usuarios (no reenviar al propio recolector ni a otros recolectores)
    destinos = [
        c
        for c in conexiones
        if c.get("rol") == "usuario" and c["connection_id"] != sender_id
    ]

    print(f"enviarUbicacion: {len(destinos)} destinos, recolector en ({rec_lat}, {rec_lng})")

    # 1) Difundir la ubicacion del recolector a todos los usuarios de la ruta
    for c in destinos:
        _post(
            client,
            c["connection_id"],
            {"action": "enviarUbicacion", "route_id": route_id, "ubicacion": ubicacion},
        )

    # 2) Detectar quienes estan a <= 3 min con MapBox Matrix y enviarles enviarAlerta
    print("Detectando usuarios a <= 3 min con MapBox Matrix...")
    cercanos = _usuarios_a_3_min(rec_lat, rec_lng, destinos)
    print(f"Usuarios cercanos: {len(cercanos)}")
    for c in cercanos:
        _post(
            client,
            c["connection_id"],
            {
                "action": "enviarAlerta",
                "route_id": route_id,
                "mensaje": "El recolector esta a menos de 3 minutos",
            },
        )

    return _ok({"enviados": len(destinos), "alertas": len(cercanos)})


# ---------------------------------------------------------------------------
# enviarAlerta: difusion manual de alerta a toda la ruta
# ---------------------------------------------------------------------------
def enviarAlerta(event, context):
    body = json.loads(event.get("body") or "{}", parse_float=Decimal)
    route_id = body.get("route_id")
    if not route_id:
        return _ok({"error": "route_id es requerido"})

    client = _api_client(event)
    sender_id = event["requestContext"]["connectionId"]

    conexiones = _conexiones_de_ruta(route_id)
    destinos = [
        c
        for c in conexiones
        if c.get("rol") == "usuario" and c["connection_id"] != sender_id
    ]

    for c in destinos:
        _post(
            client,
            c["connection_id"],
            {
                "action": "enviarAlerta",
                "route_id": route_id,
                "mensaje": body.get("mensaje", "Alerta del recolector"),
            },
        )

    return _ok({"alertas": len(destinos)})


# ---------------------------------------------------------------------------
# Auxiliares de datos / MapBox
# ---------------------------------------------------------------------------
def _conexiones_de_ruta(route_id):
    """Consulta el GSI route_id-index y devuelve todas las conexiones de la ruta."""
    items = []
    kwargs = {
        "IndexName": ROUTE_INDEX,
        "KeyConditionExpression": Key("route_id").eq(route_id),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return items


def _usuarios_a_3_min(rec_lat, rec_lng, destinos):
    """Usa la MapBox Matrix API (recolector -> usuarios) para devolver los que
    estan a <= ALERTA_SEGUNDOS de duracion de viaje."""
    if rec_lat is None or rec_lng is None or not MAPBOX_TOKEN:
        return []

    # Solo usuarios con ubicacion conocida
    con_ubic = [
        c
        for c in destinos
        if c.get("ubicacion", {}).get("lat") is not None
        and c.get("ubicacion", {}).get("lng") is not None
    ]

    print(f"_usuarios_a_3_min: {len(con_ubic)} usuarios con ubicacion conocida")
    if not con_ubic:
        return []

    # coords: primero el recolector (source), luego los usuarios (destinations)
    coords = [(rec_lng, rec_lat)] + [
        (c["ubicacion"]["lng"], c["ubicacion"]["lat"]) for c in con_ubic
    ]
    coord_str = ";".join(f"{lng},{lat}" for lng, lat in coords)
    # Incluimos el indice 0 (el propio recolector) como primer destino para
    # garantizar >= 2 elementos de matriz cuando solo hay 1 usuario (MapBox
    # rechaza matrices de 1 solo elemento). Esa primera columna sera 0
    # (recolector -> recolector) y la descartamos al leer las duraciones.
    destinations = ";".join(str(i) for i in range(0, len(coords)))

    params = urllib.parse.urlencode(
        {
            "sources": "0",
            "destinations": destinations,
            "annotations": "duration",
            "access_token": MAPBOX_TOKEN,
        }
    )
    url = (
        "https://api.mapbox.com/directions-matrix/v1/mapbox/driving/"
        f"{coord_str}?{params}"
    )

    print(f"MapBox Matrix request: {url}")

    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        print("Error al llamar a MapBox Matrix")
        # Si MapBox falla, no bloqueamos el envio de ubicacion
        return []

    print(f"MapBox Matrix response: {data}")

    # Fila del unico source. La primera columna es recolector -> recolector (0),
    # la descartamos para alinear las duraciones con la lista de usuarios.
    fila = (data.get("durations") or [[]])[0]
    durations = fila[1:]
    cercanos = []
    for c, dur in zip(con_ubic, durations):
        if dur is not None and dur <= ALERTA_SEGUNDOS:
            cercanos.append(c)
    return cercanos

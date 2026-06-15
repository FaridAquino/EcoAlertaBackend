# EcoAlertaBackend

Backend serverless en AWS (Serverless Framework v3 + Python 3.12 + DynamoDB) para EcoAlerta.
Se compone de **3 servicios independientes**, cada uno con su propio `handler.py` y `serverless.yml`:

```
EcoAlertaBackend/
├── usuarios/      → Lambdas de usuarios        | tabla: usuariosEcoAlerta
├── recolector/    → Lambdas del recolector     | tablas: recolectorEcoAlerta, rutaEcoAlerta
└── websocket/     → Lambdas de tiempo real (WS) | tabla: conexionesEcoAlerta (con GSI)
```

Cada servicio se despliega por separado (`cd <carpeta> && serverless deploy`). Todas las tablas usan
`BillingMode: PAY_PER_REQUEST` (on-demand) y el IAM está acotado al ARN específico de cada tabla.

---

## 1) usuarios

API REST (eventos `http` con CORS).

| Lambda     | Método / Ruta            | Descripción                                                                 |
|------------|--------------------------|-----------------------------------------------------------------------------|
| `register` | `POST /usuarios/register`| Registra usuario: correo, contraseña hasheada, ubicación y rutas escogidas. |
| `login`    | `POST /usuarios/login`   | Valida credenciales y devuelve correo, ubicación, `rutas` (IDs) y `rutas_detalle` (objetos completos). |

### Tabla DynamoDB: `usuariosEcoAlerta`

- **KeySchema:** `correo` → `HASH` (Partition Key)
- **Índices (GSI/LSI):** ninguno
- **BillingMode:** `PAY_PER_REQUEST`

| Atributo        | Tipo            | Descripción                                              |
|-----------------|-----------------|----------------------------------------------------------|
| `correo`        | String (S) — PK | Correo del usuario. Clave de partición.                  |
| `password_hash` | String (S)      | Contraseña hasheada con PBKDF2 (`salt$hash` en hex).     |
| `ubicacion`     | Map (M)         | `{ "lat": Number, "lng": Number }` (Decimal en DynamoDB).|
| `rutas`         | List (L)        | Rutas escogidas, p. ej. `["ruta_marini"]`.               |

---

## 2) recolector

API REST (eventos `http` con CORS).

| Lambda          | Método / Ruta                  | Descripción                                                                              |
|-----------------|--------------------------------|------------------------------------------------------------------------------------------|
| `register`      | `POST /recolector/register`    | Registra recolector: correo, contraseña hasheada y rutas.                                |
| `login`         | `POST /recolector/login`       | Valida credenciales y devuelve correo, rutas y `ruta_activa`.                            |
| `registrarRuta` | `POST /recolector/rutas`       | Crea/actualiza una ruta en `rutaEcoAlerta` (route_id, start, end, nodes, fechas).        |
| `obtenerRutas`  | `GET /recolector/rutas`        | Devuelve todas las rutas disponibles (`{ total, rutas: [...] }`).                         |
| `iniciarRuta`   | `POST /recolector/iniciar-ruta`| Valida que hoy esté dentro de `fechas` de la ruta y marca `ruta_activa` del recolector.  |

### Tabla DynamoDB: `recolectorEcoAlerta`

- **KeySchema:** `correo` → `HASH` (Partition Key)
- **Índices (GSI/LSI):** ninguno
- **BillingMode:** `PAY_PER_REQUEST`

| Atributo        | Tipo            | Descripción                                                       |
|-----------------|-----------------|-------------------------------------------------------------------|
| `correo`        | String (S) — PK | Correo del recolector. Clave de partición.                        |
| `password_hash` | String (S)      | Contraseña hasheada con PBKDF2 (`salt$hash` en hex).              |
| `rutas`         | List (L)        | Rutas asignadas al recolector.                                    |
| `ruta_activa`   | String (S)      | `route_id` de la ruta actualmente iniciada (la escribe `iniciarRuta`). |
| `iniciada_en`   | String (S)      | Timestamp ISO-8601 UTC del inicio de ruta (lo escribe `iniciarRuta`).  |

### Tabla DynamoDB: `rutaEcoAlerta`

- **KeySchema:** `route_id` → `HASH` (Partition Key)
- **Índices (GSI/LSI):** ninguno
- **BillingMode:** `PAY_PER_REQUEST`

| Atributo   | Tipo             | Descripción                                                                 |
|------------|------------------|------------------------------------------------------------------------------|
| `route_id` | String (S) — PK  | Identificador de la ruta, p. ej. `ruta_marini`. Clave de partición.          |
| `start`    | String (S)       | Punto de inicio, p. ej. `punto_1`.                                           |
| `end`      | String (S)       | Punto final, p. ej. `punto_4`.                                              |
| `nodes`    | List (L) de Map  | Nodos de la ruta: `{ "id", "label", "lat": Number, "lng": Number }`.        |
| `fechas`   | List (L)         | Días válidos de la ruta. Códigos: `L`,`M`,`MM`,`J`,`V`,`S`,`D`.              |

**Códigos de día (`fechas`):** `L`=Lunes, `M`=Martes, `MM`=Miércoles, `J`=Jueves, `V`=Viernes,
`S`=Sábado, `D`=Domingo. `iniciarRuta` compara el día actual (UTC) contra esta lista.

---

## 3) websocket

API WebSocket (API Gateway WebSocket). `routeSelectionExpression: $request.body.action`.

| Lambda            | Route key (action)| Descripción                                                                                          |
|-------------------|-------------------|------------------------------------------------------------------------------------------------------|
| `connect`         | `$connect`        | Registra la conexión: `connection_id`, `user_id`, `rol`, `route_id`, `ubicacion` (desde query params).|
| `disconnect`      | `$disconnect`     | Elimina la conexión de la tabla.                                                                      |
| `enviarUbicacion` | `enviarUbicacion` | Difunde la ubicación del recolector a los usuarios de su ruta; a los que estén a ≤3 min envía además `enviarAlerta`. |
| `enviarAlerta`    | `enviarAlerta`    | Difusión manual de alerta a todos los usuarios de la ruta.                                            |

**Proximidad (3 min):** `enviarUbicacion` usa la **MapBox Matrix API** (`directions-matrix/v1`,
`sources=0` = recolector) para obtener la duración de viaje del recolector a cada usuario; si
`duration ≤ 180s` se envía también el `action: enviarAlerta`. Requiere `MAPBOX_TOKEN` (vía `.env`).

### Tabla DynamoDB: `conexionesEcoAlerta`

- **KeySchema:** `connection_id` → `HASH` (Partition Key)
- **BillingMode:** `PAY_PER_REQUEST`
- **Índice GSI: `route_id-index`**
  - **Atributo indexado:** `route_id` → `HASH`
  - **Projection:** `INCLUDE` → [`user_id`, `rol`, `ubicacion`] (proyección mínima para abaratar costo)
  - **Uso:** consultar (`Query`) todas las conexiones de una misma ruta para hacer el broadcast.

| Atributo        | Tipo            | Indexado                  | Descripción                                                  |
|-----------------|-----------------|---------------------------|--------------------------------------------------------------|
| `connection_id` | String (S) — PK | KeySchema tabla (HASH)    | ID de conexión WebSocket (de `requestContext.connectionId`). |
| `route_id`      | String (S)      | **GSI `route_id-index` (HASH)** | Ruta a la que pertenece la conexión. Indexado para broadcast.|
| `user_id`       | String (S)      | Proyectado en el GSI      | Identificador del usuario/recolector.                        |
| `rol`           | String (S)      | Proyectado en el GSI      | `"usuario"` o `"recolector"`.                                |
| `ubicacion`     | Map (M)         | Proyectado en el GSI      | `{ "lat": Number, "lng": Number }` (Decimal).                |

---

## Ejemplos de prueba (JSON)

> Reemplaza `https://API_ID.execute-api.us-east-1.amazonaws.com/dev` por la URL que imprime
> `serverless deploy` de cada servicio. Para WebSocket, usa la URL `wss://...`.

### usuarios

**POST /usuarios/register**
```json
{
  "correo": "ana@correo.com",
  "password": "MiClave123",
  "latitud": -11.419556,
  "longitud": -75.697665,
  "rutas": ["ruta_marini"]
}
```

**POST /usuarios/login**
```json
{
  "correo": "ana@correo.com",
  "password": "MiClave123"
}
```

```bash
curl -X POST https://API_ID.execute-api.us-east-1.amazonaws.com/dev/usuarios/login \
  -H "Content-Type: application/json" \
  -d '{"correo":"ana@correo.com","password":"MiClave123"}'
```

Respuesta (el frontend usa `rutas_detalle` para Calendario, Rutas y Seguimiento):
```json
{
  "mensaje": "login correcto",
  "correo": "ana@correo.com",
  "ubicacion": { "lat": -11.419556, "lng": -75.697665 },
  "rutas": ["ruta_marini"],
  "rutas_detalle": [
    {
      "route_id": "ruta_marini",
      "start": "punto_1",
      "end": "punto_4",
      "nodes": [
        { "id": "A", "label": "1erPunto", "lat": -11.419556, "lng": -75.697665 }
      ],
      "fechas": ["L", "M", "D"]
    }
  ]
}
```
- **Calendario** → `rutas_detalle[].fechas`
- **Rutas** → `rutas_detalle[].nodes` (`start`/`end`)
- **Seguimiento** → `rutas_detalle[].route_id` (para conectar al WebSocket con ese `route_id`)

### recolector

**POST /recolector/register**
```json
{
  "correo": "carlos@correo.com",
  "password": "Recolector2026",
  "rutas": ["ruta_marini", "ruta_san_ramon"]
}
```

**POST /recolector/login**
```json
{
  "correo": "carlos@correo.com",
  "password": "Recolector2026"
}
```

**POST /recolector/rutas** (registrarRuta)
```json
{
  "route_id": "ruta_marini",
  "start": "punto_1",
  "end": "punto_4",
  "nodes": [
    { "id": "A", "label": "1erPunto", "lat": -11.419556, "lng": -75.697665 },
    { "id": "B", "label": "2doPunto", "lat": -11.41894,  "lng": -75.697676 },
    { "id": "C", "label": "3erPunto", "lat": -11.418789, "lng": -75.699746 },
    { "id": "D", "label": "4toPunto", "lat": -11.419409, "lng": -75.699832 }
  ],
  "fechas": ["L", "M", "D"]
}
```

Segundo ejemplo (`ruta_san_ramon`):
```json
{
  "route_id": "ruta_san_ramon",
  "start": "punto_1",
  "end": "punto_4",
  "nodes": [
    { "id": "A", "label": "1erPunto", "lat": -11.411656, "lng": -75.682757 },
    { "id": "B", "label": "2doPunto", "lat": -11.410429, "lng": -75.682623 },
    { "id": "C", "label": "3erPunto", "lat": -11.410445, "lng": -75.684176 },
    { "id": "D", "label": "4toPunto", "lat": -11.408473, "lng": -75.684205 }
  ],
  "fechas": ["M", "MM", "S"]
}
```

**GET /recolector/rutas** (obtenerRutas) — sin body, devuelve todas las rutas:
```bash
curl https://API_ID.execute-api.us-east-1.amazonaws.com/dev/recolector/rutas
```
Respuesta:
```json
{
  "total": 2,
  "rutas": [
    { "route_id": "ruta_marini", "start": "punto_1", "end": "punto_4", "nodes": [ ... ], "fechas": ["L","M","D"] },
    { "route_id": "ruta_san_ramon", "start": "punto_1", "end": "punto_4", "nodes": [ ... ], "fechas": ["M","MM","S"] }
  ]
}
```

**POST /recolector/iniciar-ruta** (iniciarRuta)
```json
{
  "correo": "carlos@correo.com",
  "route_id": "ruta_marini"
}
```
> Solo inicia si **hoy** (hora de Perú, UTC-5) está en `fechas` de esa ruta; si no, responde 400
> con `dia_actual` y `dias_validos`.

### websocket

La conexión se hace contra la URL `wss://...` pasando los datos como **query params**
(no body). Puedes probar con `wscat`:

```bash
# Conectar como recolector
wscat -c "wss://WS_ID.execute-api.us-east-1.amazonaws.com/dev?user_id=carlos&rol=recolector&route_id=ruta_marini&lat=-11.4195&lng=-75.6976"

# Conectar como usuario (en otra terminal)
wscat -c "wss://WS_ID.execute-api.us-east-1.amazonaws.com/dev?user_id=ana&rol=usuario&route_id=ruta_marini&lat=-11.4188&lng=-75.6996"
```

Una vez conectado, los mensajes se envían como JSON con el campo `action`:

**enviarUbicacion** (lo manda el recolector; difunde su ubicación y dispara alerta a ≤3 min):
```json
{
  "action": "enviarUbicacion",
  "route_id": "ruta_marini",
  "ubicacion": { "lat": -11.4195, "lng": -75.6976 }
}
```

**enviarAlerta** (difusión manual de alerta a toda la ruta):
```json
{
  "action": "enviarAlerta",
  "route_id": "ruta_marini",
  "mensaje": "El recolector va llegando"
}
```

Mensajes que **recibe** el frontend del usuario:
```json
{ "action": "enviarUbicacion", "route_id": "ruta_marini", "ubicacion": { "lat": -11.4195, "lng": -75.6976 } }
{ "action": "enviarAlerta", "route_id": "ruta_marini", "mensaje": "El recolector esta a menos de 3 minutos" }
```

---

## Resumen: qué Lambda usa qué tabla

| Servicio   | Lambda            | Tabla(s)                          | Operaciones IAM                         |
|------------|-------------------|-----------------------------------|-----------------------------------------|
| usuarios   | register          | `usuariosEcoAlerta`               | `PutItem`                                |
| usuarios   | login             | `usuariosEcoAlerta`, `rutaEcoAlerta` (solo lectura) | `GetItem`, `BatchGetItem`  |
| recolector | register, login   | `recolectorEcoAlerta`             | `PutItem`, `GetItem`, `UpdateItem`       |
| recolector | registrarRuta     | `rutaEcoAlerta`                   | `PutItem`                                |
| recolector | obtenerRutas      | `rutaEcoAlerta`                   | `Scan`                                   |
| recolector | iniciarRuta       | `rutaEcoAlerta`, `recolectorEcoAlerta` | `GetItem`, `UpdateItem`             |
| websocket  | connect/disconnect| `conexionesEcoAlerta`             | `PutItem`, `DeleteItem`                  |
| websocket  | enviarUbicacion / enviarAlerta | `conexionesEcoAlerta` (+ GSI) | `Query` (GSI), `DeleteItem`, `execute-api:ManageConnections` |

---

## Despliegue

Requisitos: Node.js + Serverless Framework v3, credenciales AWS y Python 3.12.

```bash
# Variables de entorno del websocket (token de MapBox)
# websocket/.env  →  MAPBOX_TOKEN=pk.tu_token_default   (sin comillas)

cd usuarios   && serverless deploy
cd ../recolector && serverless deploy
cd ../websocket  && serverless deploy   # lee MAPBOX_TOKEN del .env (useDotenv: true)
```

### Optimización de costos
- Todas las tablas en `PAY_PER_REQUEST` (sin capacidad provisionada).
- Un único GSI (`route_id-index`) con proyección `INCLUDE` mínima, no `ALL`.
- IAM de mínimo privilegio: cada rol referencia solo el ARN de su(s) tabla(s)/GSI, nunca `Resource: "*"`.

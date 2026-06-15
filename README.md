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
| `login`    | `POST /usuarios/login`   | Valida credenciales y devuelve correo, ubicación y rutas (sin el hash).      |

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

## Resumen: qué Lambda usa qué tabla

| Servicio   | Lambda            | Tabla(s)                          | Operaciones IAM                         |
|------------|-------------------|-----------------------------------|-----------------------------------------|
| usuarios   | register, login   | `usuariosEcoAlerta`               | `PutItem`, `GetItem`                     |
| recolector | register, login   | `recolectorEcoAlerta`             | `PutItem`, `GetItem`, `UpdateItem`       |
| recolector | registrarRuta     | `rutaEcoAlerta`                   | `PutItem`                                |
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

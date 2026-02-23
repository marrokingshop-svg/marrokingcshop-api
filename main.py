from fastapi import FastAPI, Body, HTTPException, Depends, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import os
import psycopg2
import requests
import bcrypt
import asyncio
import time
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from jose import jwt, JWTError
from datetime import datetime, timedelta

app = FastAPI(title="Marrokingcshop System Pro")

# =====================================================
# CONFIGURACIÓN
# =====================================================
MELI_CLIENT_ID = os.environ.get("MELI_CLIENT_ID")
MELI_CLIENT_SECRET = os.environ.get("MELI_CLIENT_SECRET")
MELI_REDIRECT_URI = os.environ.get("MELI_REDIRECT_URI")

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)

SECRET_KEY = os.environ.get("SECRET_KEY", "MARROKING_SECRET_2025_1")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440
security = HTTPBearer()

# =====================================================
# CORS Y WEBSOCKETS (EL TÚNEL EN TIEMPO REAL 🚇)
# =====================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Nuestro "Megáfono" para avisarle al Frontend
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

manager = ConnectionManager()

@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Mantenemos el túnel abierto escuchando
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.api_route("/{path_name:path}", methods=["OPTIONS"])
async def handle_options(request: Request, path_name: str):
    return {}

@app.get("/health")
def health():
    return {"status": "online"}

# =====================================================
# BASE DE DATOS
# =====================================================
def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise Exception("DATABASE_URL no configurada")
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)

@app.on_event("startup")
def startup_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT,
            price NUMERIC,
            stock INTEGER,
            meli_id TEXT UNIQUE,
            status TEXT DEFAULT 'active'
        );
    """)

    cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS item_id TEXT;")
    cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS variation_id TEXT;")
    cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS thumbnail TEXT;")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS credentials (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT
        );
    """)

    conn.commit()
    conn.close()
    print("✅ Base de datos actualizada y lista.")

# =====================================================
# SEGURIDAD
# =====================================================
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")

# =====================================================
# RECEPTOR DE NOTIFICACIONES AUTOMÁTICAS (WEBHOOK) 🚀
# =====================================================
@app.api_route("/meli/notifications", methods=["POST", "GET"])
async def meli_notifications(request: Request, background_tasks: BackgroundTasks):
    try:
        if request.method == "GET":
            return {"status": "ok"}

        data = await request.json()
        resource = data.get("resource") 
        topic = data.get("topic")

        if topic in ["items", "orders_v2", "orders"]:
            print(f"🔔 Notificación recibida: {topic} en {resource}")
            
            # 📢 ¡EL MEGÁFONO EN ACCIÓN! Le avisamos a tu index.html
            meli_item_id = resource.split("/")[-1]
            mensaje_alerta = f"¡Venta o cambio detectado en {meli_item_id}!" if "order" in topic else f"Stock actualizado en {meli_item_id}"
            
            await manager.broadcast({
                "type": "notification",
                "title": "🔔 Mercado Libre",
                "message": mensaje_alerta
            })

            background_tasks.add_task(sync_single_resource, resource)
        
        return {"status": "received"}
    except Exception as e:
        print(f"❌ Error recibiendo notificación: {e}")
        return {"status": "error"}

def sync_single_resource(resource):
    # 🕒 Esperamos 10 segundos para que Meli actualice sus datos reales
    time.sleep(10) 
    
    conn = None
    try:
        meli_item_id = resource.split("/")[-1]
        print(f"🔄 Iniciando sincronización de fondo para: {meli_item_id}")
        
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        token_row = cur.fetchone()
        if not token_row: 
            print("❌ No se encontró token en DB")
            return

        token = token_row["value"]
        headers = {"Authorization": f"Bearer {token}"}

        # Usamos el timestamp para saltar el caché de la API de Meli
        ts = int(datetime.utcnow().timestamp())
        resp = requests.get(f"https://api.mercadolibre.com/items/{meli_item_id}?ts={ts}", headers=headers)
        
        if resp.status_code == 200:
            item = resp.json()
            status_real = item.get("status", "active")
            price = item.get("price") or 0
            thumbnail = item.get("thumbnail", "")

            if item.get("variations"):
                for var in item["variations"]:
                    stock_var = var.get("available_quantity", 0)
                    meli_var_id = f"{meli_item_id}-{var['id']}"
                    cur.execute("""
                        UPDATE products 
                        SET stock = %s, price = %s, status = %s, thumbnail = %s
                        WHERE meli_id = %s
                    """, (stock_var, price, status_real, thumbnail, meli_var_id))
            else:
                stock_global = item.get("available_quantity", 0)
                cur.execute("""
                    UPDATE products 
                    SET stock = %s, price = %s, status = %s, thumbnail = %s
                    WHERE meli_id = %s
                """, (stock_global, price, status_real, thumbnail, meli_item_id))
            
            conn.commit()
            print(f"✅ DB ACTUALIZADA para {meli_item_id}")
        else:
            print(f"⚠️ Meli respondió con error {resp.status_code}")

    except Exception as e:
        print(f"❌ Error crítico en sync_single_resource: {e}")
    finally:
        if conn:
            conn.close()
            print("🔌 Conexión cerrada en tarea de fondo.")

# =====================================================
# AUTH MERCADO LIBRE
# =====================================================
@app.get("/auth/callback")
async def meli_callback(code: str = None):
    if not code:
        return {"status": "error", "message": "Falta code"}

    url = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": MELI_CLIENT_ID,
        "client_secret": MELI_CLIENT_SECRET,
        "code": code,
        "redirect_uri": MELI_REDIRECT_URI
    }

    resp = requests.post(url, data=payload)
    data = resp.json()

    if "access_token" in data:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO credentials (key, value)
            VALUES ('access_token', %s), ('user_id', %s)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value
        """, (data["access_token"], str(data["user_id"])))

        conn.commit()
        conn.close()

        return {"status": "success", "message": "Conectado a Mercado Libre"}

    return {"status": "error", "detail": data}

# =====================================================
# SINCRONIZACIÓN MANUAL
# =====================================================
@app.post("/meli/sync")
def sync_meli_products(user=Depends(get_current_user)):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        token_row = cur.fetchone()
        cur.execute("SELECT value FROM credentials WHERE key='user_id'")
        user_row = cur.fetchone()

        if not token_row or not user_row:
            raise HTTPException(status_code=400, detail="Mercado Libre no vinculado. Clic en 'Vincular Cuenta'.")

        token = token_row["value"]
        user_id = user_row["value"]
        headers = {"Authorization": f"Bearer {token}"}

        url_search = f"https://api.mercadolibre.com/users/{user_id}/items/search"
        params = {"search_type": "scan", "limit": 100, "status": "active,paused,not_yet_active"}
        
        r_search = requests.get(url_search, headers=headers, params=params)
        
        if r_search.status_code != 200:
            raise HTTPException(status_code=400, detail="El token expiró o falló la conexión con ML.")

        data_search = r_search.json()
        items_ids = data_search.get("results", [])
        scroll_id = data_search.get("scroll_id")

        while scroll_id:
            r_scroll = requests.get(url_search, headers=headers, params={"search_type": "scan", "scroll_id": scroll_id})
            if r_scroll.status_code != 200: break
            d_scroll = r_scroll.json()
            results = d_scroll.get("results", [])
            if not results: break
            items_ids.extend(results)
            scroll_id = d_scroll.get("scroll_id")

        if not items_ids:
            return {"status": "success", "sincronizados": 0, "message": "No hay productos en ML"}

        productos_a_guardar = []
        for m_id in items_ids:
            detail_resp = requests.get(f"https://api.mercadolibre.com/items/{m_id}", headers=headers)
            if detail_resp.status_code != 200: continue 
                
            item = detail_resp.json()
            status_real = item.get("status", "active")
            price = item.get("price") or 0
            thumbnail_url = item.get("thumbnail", "") 

            if item.get("variations"):
                for var in item["variations"]:
                    stock_var = var.get("available_quantity", 0)
                    attrs = " - ".join([a["value_name"] for a in var.get("attribute_combinations", [])])
                    name_var = f"{item['title']} ({attrs})"
                    meli_var_id = f"{m_id}-{var['id']}"
                    productos_a_guardar.append((name_var, price, stock_var, meli_var_id, status_real, thumbnail_url))
            else:
                stock_global = item.get("available_quantity", 0)
                productos_a_guardar.append((item["title"], price, stock_global, m_id, status_real, thumbnail_url))

        if productos_a_guardar:
            cur.execute("TRUNCATE TABLE products RESTART IDENTITY CASCADE;")
            for prod in productos_a_guardar:
                cur.execute("""
                    INSERT INTO products (name, price, stock, meli_id, status, thumbnail)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, prod)
            conn.commit()
            return {"status": "success", "sincronizados": len(productos_a_guardar)}
        else:
            raise HTTPException(status_code=400, detail="No se pudo extraer el detalle de los productos.")

    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

# =====================================================
# PRODUCTOS
# =====================================================
@app.get("/products-grouped")
def get_products_grouped(user = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, price, stock, meli_id, status, thumbnail
        FROM products
        ORDER BY name
    """)

    rows = cur.fetchall()
    conn.close()

    products = []
    for r in rows:
        if not r["meli_id"]: continue
        products.append({
            "title": r["name"],
            "status": r["status"],
            "meli_item_id": r["meli_id"],
            "price": float(r["price"]) if r["price"] else 0,
            "stock": r["stock"] if r["stock"] is not None else 0,
            "thumbnail": r["thumbnail"] or "",
            "variations": [] 
        })
    return {"products": products}

# =====================================================
# ACTUALIZAR STOCK
# =====================================================
class StockUpdate(BaseModel):
    new_stock: int

@app.put("/meli/update_stock/{meli_id}")
def update_stock_meli(meli_id: str, stock_data: StockUpdate, user=Depends(get_current_user)):
    new_quantity = stock_data.new_stock
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        token_row = cur.fetchone()
        if not token_row:
            raise HTTPException(status_code=400, detail="No hay token vinculado")
        
        token = token_row["value"]
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/json"
        }

        if "-" in meli_id:
            parts = meli_id.split("-")
            item_id = parts[0]
            variation_id = int("".join(filter(str.isdigit, parts[1])))
            url_api = f"https://api.mercadolibre.com/items/{item_id}/variations/{variation_id}"
            payload = {"available_quantity": new_quantity}
        else:
            url_api = f"https://api.mercadolibre.com/items/{meli_id}"
            payload = {"available_quantity": new_quantity}

        response = requests.put(url_api, headers=headers, json=payload)

        if response.status_code not in [200, 201]:
            error_data = response.json()
            raise HTTPException(status_code=response.status_code, detail=error_data.get('message'))

        cur.execute("UPDATE products SET stock = %s WHERE meli_id = %s", (new_quantity, meli_id))
        conn.commit()
        return {"status": "success"}

    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

# =====================================================
# LOGIN
# =====================================================
@app.post("/login")
def login(username: str = Body(...), password: str = Body(...)):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=400, detail="Credenciales inválidas")

    try:
        password_bytes = password.encode('utf-8')
        hashed_password = user["password"].encode('utf-8')

        if not bcrypt.checkpw(password_bytes, hashed_password):
            raise HTTPException(status_code=400, detail="Credenciales inválidas")
    except Exception as e:
        if not pwd_context.verify(password, user["password"]):
            raise HTTPException(status_code=400, detail="Credenciales inválidas")

    token = create_access_token({
        "sub": user["username"],
        "role": user["role"]
    })
    
    return {"access_token": token, "token_type": "bearer"}
from fastapi import FastAPI, Body, HTTPException, Depends, Request, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from fastapi import Response
import os
import psycopg2
import requests
import bcrypt
import asyncio
import time
import json
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

# =====================================================
# AUTOMATIZACIÓN DE TOKEN (MEJORA PRO 🔐)
# =====================================================
def refresh_meli_token_db():
    """Refresca el token usando la base de datos para autonomía total"""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM credentials WHERE key='refresh_token'")
        row = cur.fetchone()
        if not row:
            return None

        refresh_token = row["value"]
        url = "https://api.mercadolibre.com/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": MELI_CLIENT_ID,
            "client_secret": MELI_CLIENT_SECRET,
            "refresh_token": refresh_token
        }
        
        resp = requests.post(url, data=payload)
        if resp.status_code == 200:
            data = resp.json()
            # Guardamos el nuevo access y el nuevo refresh
            cur.execute("""
                INSERT INTO credentials (key, value)
                VALUES ('access_token', %s), ('refresh_token', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (data["access_token"], data["refresh_token"]))
            conn.commit()
            print("✅ Token de MeLi refrescado automáticamente en DB")
            return data["access_token"]
        return None
    except Exception as e:
        print(f"❌ Error refrescando token: {e}")
        return None
    finally:
        conn.close()

# =====================================================
# INICIALIZACIÓN DB
# =====================================================
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
            status TEXT DEFAULT 'active',
            item_id TEXT,
            variation_id TEXT,
            thumbnail TEXT
        );
    """)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS work_credentials (
            id SERIAL PRIMARY KEY,
            site_name TEXT,
            email_user TEXT,
            password_val TEXT,
            category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.commit()
    conn.close()
    print("✅ Base de datos Marroking System Pro Lista")

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
# RECEPTOR DE NOTIFICACIONES (WEBHOOK)
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
            print(f"🔔 Notificación: {topic} en {resource}")
            
            meli_item_id = resource.split("/")[-1]
            mensaje_alerta = f"Venta detectada en {meli_item_id}" if "order" in topic else f"Cambio en {meli_item_id}"
            
            await manager.broadcast({
                "type": "notification",
                "title": "🔔 Mercado Libre",
                "message": mensaje_alerta
            })

            background_tasks.add_task(sync_single_resource, resource)
        
        return {"status": "received"}
    except Exception as e:
        print(f"❌ Error webhook: {e}")
        return {"status": "error"}

async def sync_single_resource(resource):
    await asyncio.sleep(10) 
    
    conn = None
    try:
        meli_item_id = resource.split("/")[-1]
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        token_row = cur.fetchone()
        if not token_row: return

        token = token_row["value"]
        headers = {"Authorization": f"Bearer {token}"}
        url = f"https://api.mercadolibre.com/items/{meli_item_id}"
        
        resp = requests.get(url, headers=headers)
        
        if resp.status_code == 401:
            nuevo_token = refresh_meli_token_db()
            if nuevo_token:
                headers["Authorization"] = f"Bearer {nuevo_token}"
                resp = requests.get(url, headers=headers)

        if resp.status_code == 200:
            item = resp.json()
            price = item.get("price", 0)
            status_real = item.get("status", "active")
            thumbnail = item.get("thumbnail", "")

            if item.get("variations"):
                for var in item["variations"]:
                    stock_var = var.get("available_quantity", 0)
                    meli_var_id = f"{meli_item_id}-{var['id']}"
                    cur.execute("""
                        UPDATE products SET stock = %s, price = %s, status = %s, thumbnail = %s 
                        WHERE meli_id = %s
                    """, (stock_var, price, status_real, thumbnail, meli_var_id))
            else:
                stock_global = item.get("available_quantity", 0)
                cur.execute("""
                    UPDATE products SET stock = %s, price = %s, status = %s, thumbnail = %s 
                    WHERE meli_id = %s
                """, (stock_global, price, status_real, thumbnail, meli_item_id))
            
            conn.commit()
            print(f"✅ Sincronizado en DB: {meli_item_id}")
            await manager.broadcast({"type": "sync_complete"})
            
    except Exception as e:
        print(f"❌ Error sync: {e}")
    finally:
        if conn: conn.close()

# =====================================================
# AUTH MERCADO LIBRE (CALLBACK MEJORADO)
# =====================================================
@app.get("/auth/callback")
async def meli_callback(code: str = None):
    if not code: return {"status": "error"}

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
            VALUES ('access_token', %s), ('user_id', %s), ('refresh_token', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (data["access_token"], str(data["user_id"]), data["refresh_token"]))
        conn.commit()
        conn.close()
        return {"status": "success", "message": "Vinculación Exitosa"}
    return {"status": "error", "detail": data}

# =====================================================
# SINCRONIZACIÓN MANUAL
# =====================================================
@app.post("/meli/sync")
async def sync_meli_products(user=Depends(get_current_user)):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        t_row = cur.fetchone()
        cur.execute("SELECT value FROM credentials WHERE key='user_id'")
        u_row = cur.fetchone()

        if not t_row: raise HTTPException(status_code=400, detail="Vincule su cuenta primero.")

        token = t_row["value"]
        user_id = u_row["value"]
        headers = {"Authorization": f"Bearer {token}"}

        r = requests.get(f"https://api.mercadolibre.com/users/{user_id}/items/search", headers=headers, params={"limit": 100})
        
        if r.status_code == 401:
            nuevo_token = refresh_meli_token_db()
            if nuevo_token:
                headers["Authorization"] = f"Bearer {nuevo_token}"
                r = requests.get(f"https://api.mercadolibre.com/users/{user_id}/items/search", headers=headers, params={"limit": 100})

        ids = r.json().get("results", [])
        if not ids: return {"status": "success", "sincronizados": 0}

        count = 0
        for m_id in ids:
            det = requests.get(f"https://api.mercadolibre.com/items/{m_id}", headers=headers).json()
            
            name = det.get("title")
            price = det.get("price")
            status = det.get("status")
            thumbnail = det.get("thumbnail")

            if det.get("variations"):
                for var in det["variations"]:
                    v_id = f"{m_id}-{var['id']}"
                    v_stock = var.get("available_quantity")
                    cur.execute("""
                        INSERT INTO products (name, price, stock, meli_id, status, thumbnail)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (meli_id) DO UPDATE SET 
                            stock = EXCLUDED.stock, price = EXCLUDED.price, status = EXCLUDED.status, thumbnail = EXCLUDED.thumbnail
                    """, (name, price, v_stock, v_id, status, thumbnail))
            else:
                stock = det.get("available_quantity")
                cur.execute("""
                    INSERT INTO products (name, price, stock, meli_id, status, thumbnail)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (meli_id) DO UPDATE SET 
                        stock = EXCLUDED.stock, price = EXCLUDED.price, status = EXCLUDED.status, thumbnail = EXCLUDED.thumbnail
                """, (name, price, stock, m_id, status, thumbnail))
            count += 1
        
        conn.commit()
        return {"status": "success", "sincronizados": count}
        
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Error en Sync: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

# =====================================================
# ACTUALIZAR STOCK (LA VERDADERA SOLUCIÓN A LA ROPA 👕)
# =====================================================
class StockUpdate(BaseModel):
    new_stock: int

@app.put("/meli/update_stock/{meli_id}")
def update_stock_meli(meli_id: str, stock_data: StockUpdate, user=Depends(get_current_user)):
    new_quantity = stock_data.new_stock
    print(f"\n🚀 ACTUALIZANDO STOCK: {meli_id} -> {new_quantity}", flush=True)
    
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM credentials WHERE key='access_token'")
        token_row = cur.fetchone()
        if not token_row: 
            raise HTTPException(status_code=400, detail="Vincule MeLi primero")
        
        token = token_row["value"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 🛠️ EL SECRETO PARA LA ROPA: La forma correcta de enviar las tallas
        if "-" in meli_id:
            parts = meli_id.split("-")
            item_id = parts[0]
            var_id = int("".join(filter(str.isdigit, parts[1])))
            
            # Para prendas, MeLi pide que se haga desde el producto padre
            url_api = f"https://api.mercadolibre.com/items/{item_id}"
            payload = {
                "variations": [
                    {
                        "id": var_id,
                        "available_quantity": new_quantity
                    }
                ]
            }
        else:
            # Para perfumes o productos sin variaciones
            url_api = f"https://api.mercadolibre.com/items/{meli_id}"
            payload = {"available_quantity": new_quantity}
        
        # Enviamos la petición
        response = requests.put(url_api, headers=headers, json=payload)

        # 🔄 REINTENTO SI EL TOKEN MURIÓ
        if response.status_code == 401:
            print("⚠️ Token vencido. Intentando autorefresco...", flush=True)
            nuevo_token = refresh_meli_token_db()
            if nuevo_token:
                headers["Authorization"] = f"Bearer {nuevo_token}"
                response = requests.put(url_api, headers=headers, json=payload)

        # 🔍 SI FALLA, LANZAMOS EL LOG DETALLADO
        if response.status_code not in [200, 201]:
            error_data = response.json()
            print("\n" + "❌"*10 + " ERROR DE MERCADO LIBRE " + "❌"*10, flush=True)
            print(f"CÓDIGO: {response.status_code}", flush=True)
            print(f"RESPUESTA: {json.dumps(error_data, indent=2)}", flush=True)
            print("❌"*30 + "\n", flush=True)
            raise HTTPException(status_code=response.status_code, detail=error_data.get('message'))

        # Si todo sale perfecto, guardamos en la base de datos de Render
        cur.execute("UPDATE products SET stock = %s WHERE meli_id = %s", (new_quantity, meli_id))
        conn.commit()
        print(f"⭐ Stock actualizado exitosamente en Mercado Libre para {meli_id}", flush=True)
        return {"status": "success"}

    except Exception as e:
        if conn: conn.rollback()
        if isinstance(e, HTTPException):
            raise e
        print(f"💥 ERROR INESPERADO: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn: conn.close()

# =====================================================
# PRODUCTOS Y LOGIN
# =====================================================
@app.get("/products-grouped")
def get_products_grouped(response: Response, user = Depends(get_current_user)):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, price, stock, meli_id, status, thumbnail FROM products ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    products = [{"title": r["name"], "status": r["status"], "meli_item_id": r["meli_id"], 
                "price": float(r["price"]), "stock": r["stock"], "thumbnail": r["thumbnail"], "variations": []} 
               for r in rows if r["meli_id"]]
    return {"products": products}

class WorkCredential(BaseModel):
    site_name: str
    email_user: str
    password_val: str
    category: str

@app.get("/work-credentials")
def get_work_credentials(user=Depends(get_current_user)):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT id, site_name, email_user, password_val, category FROM work_credentials ORDER BY site_name ASC")
    rows = cur.fetchall(); conn.close()
    return {"credentials": rows}

@app.post("/work-credentials")
def add_work_credential(cred: WorkCredential, user=Depends(get_current_user)):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO work_credentials (site_name, email_user, password_val, category) VALUES (%s, %s, %s, %s)", 
                (cred.site_name, cred.email_user, cred.password_val, cred.category))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.delete("/work-credentials/{cred_id}")
def delete_work_credential(cred_id: int, user=Depends(get_current_user)):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM work_credentials WHERE id = %s", (cred_id,))
    conn.commit(); conn.close()
    return {"status": "success"}

@app.post("/login")
def login(username: str = Body(...), password: str = Body(...)):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=%s", (username,))
    user = cur.fetchone(); conn.close()
    if not user or not bcrypt.checkpw(password.encode('utf-8'), user["password"].encode('utf-8')):
        raise HTTPException(status_code=400, detail="Error")
    token = create_access_token({"sub": user["username"], "role": user["role"]})
    return {"access_token": token, "token_type": "bearer"}
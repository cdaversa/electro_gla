from flask import Flask, render_template, request, redirect, url_for, send_file, flash, session
import sqlite3, os, json, shutil, urllib.parse
import pandas as pd
import pytz
from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash
from zipfile import ZipFile   # ✅ Para crear backups en ZIP

DB_NAME = "inventario.db"
JSON_FILE = "productos_stock.json"

app = Flask(__name__)
app.secret_key = "inventario_secret"

# --- FILTRO PARA FORMATEAR PRECIOS ---
@app.template_filter('precio')
def format_precio(value):
    try:
        return f"{float(value):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except:
        return value

# --- FUNCIÓN PARA CONVERTIR PRECIOS A FLOAT ---
def normalizar_precio(valor):
    """
    Convierte precios en formato '47.473,63', '47473,63', '47.473.63' o '47473.63' a float.
    Corrige separadores de miles y deja solo el decimal.
    """
    if not valor:
        return 0.0
    
    valor = str(valor).strip()

    # ✅ Eliminar caracteres no numéricos excepto , y .
    import re
    valor = re.sub(r"[^0-9,\.]", "", valor)

    # ✅ Si hay más de un punto, eliminamos todos menos el último
    if valor.count('.') > 1:
        partes = valor.split('.')
        valor = ''.join(partes[:-1]) + '.' + partes[-1]

    # ✅ Si hay una coma, la usamos como decimal (y eliminamos puntos)
    if ',' in valor:
        valor = valor.replace('.', '').replace(',', '.')

    try:
        return float(valor)
    except ValueError:
        return 0.0

# --- CONEXIÓN DB ---
def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    conn.execute('''
    CREATE TABLE IF NOT EXISTS productos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        cantidad INTEGER NOT NULL,
        stock_minimo INTEGER NOT NULL,
        precio_costo REAL NOT NULL,
        proveedor TEXT NOT NULL,
        ganancia REAL DEFAULT 0
    )''')

    # Si falta la columna ganancia, agregarla
    try:
        conn.execute("ALTER TABLE productos ADD COLUMN ganancia REAL DEFAULT 0")
    except:
        pass

    # ✅ Crear tabla usuarios si no existe
    conn.execute('''
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )''')

    # ✅ Crear usuario admin por defecto si no existe
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM usuarios")
    if cur.fetchone()[0] == 0:
        from werkzeug.security import generate_password_hash
        cur.execute("INSERT INTO usuarios (usuario, password) VALUES (?, ?)",
                    ("admin", generate_password_hash("admin123")))

    conn.commit()
    conn.close()
    migrate_from_json()

# --- MIGRACIÓN DESDE JSON SI EXISTE ---
def migrate_from_json():
    if os.path.exists(JSON_FILE):
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM productos")
        if cur.fetchone()[0] == 0:
            with open(JSON_FILE, "r") as f:
                productos = json.load(f)
            for p in productos:
                cur.execute("INSERT INTO productos (nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia) VALUES (?,?,?,?,?,0)",
                            (p["nombre"],p["cantidad"],p["stock_minimo"],p["precio_costo"],p["proveedor"]))
            conn.commit()
        conn.close()

# --- CONEXIÓN DB LOGS ---
LOG_DB = "logs.db"

def get_connection_logs():
    conn = sqlite3.connect(LOG_DB)
    conn.row_factory = sqlite3.Row
    return conn

# --- Inicialización log.db ---

def init_logs_table():
    conn = get_connection_logs()
    cur = conn.cursor()
    # ✅ Crear la tabla solo con los campos necesarios
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT,
            accion TEXT,
            detalle TEXT,
            fecha TEXT
        )
    """)
    conn.commit()
    conn.close()

# --- Registrar SOLO ventas ---
def registrar_log(usuario, producto, cantidad, total):
    conn = get_connection_logs()
    cur = conn.cursor()
    zona_arg = pytz.timezone("America/Argentina/Buenos_Aires")
    fecha = datetime.now(zona_arg).strftime("%Y-%m-%d %H:%M:%S")
    detalle = f"Venta de {cantidad} x {producto} - Total ${total:.2f}"
    cur.execute("INSERT INTO logs (usuario, accion, detalle, fecha) VALUES (?, ?, ?, ?)",
                (usuario, "VENTA", detalle, fecha))
    conn.commit()
    conn.close()

# --- FUNCIONES DE BD ---
def obtener_productos():
    conn = get_connection()
    productos = conn.execute("SELECT * FROM productos ORDER BY LOWER(nombre)").fetchall()
    conn.close()
    return productos

def obtener_producto(id):
    conn = get_connection()
    p = conn.execute("SELECT * FROM productos WHERE id=?", (id,)).fetchone()
    conn.close()
    return p

def agregar_producto(nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia):
    conn = get_connection()
    conn.execute("INSERT INTO productos (nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia) VALUES (?,?,?,?,?,?)",
                 (nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia))
    conn.commit()
    conn.close()

def actualizar_producto(id,nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia):
    conn = get_connection()
    conn.execute("UPDATE productos SET nombre=?, cantidad=?, stock_minimo=?, precio_costo=?, proveedor=?, ganancia=? WHERE id=?",
                 (nombre,cantidad,stock_minimo,precio_costo,proveedor,ganancia,id))
    conn.commit()
    conn.close()

def eliminar_producto(id):
    conn = get_connection()
    conn.execute("DELETE FROM productos WHERE id=?", (id,))
    conn.commit()
    conn.close()

def registrar_venta_por_nombre(nombre, cantidad):
    conn = get_connection()
    producto = conn.execute("SELECT * FROM productos WHERE nombre=?", (nombre,)).fetchone()

    if not producto:
        conn.close()
        return False  # Producto no existe

    if producto["cantidad"] < cantidad:
        conn.close()
        return None  # No hay stock suficiente

    # ✅ Si hay stock, calcular total y registrar en logs
    total = producto["precio_costo"] * cantidad
    registrar_log(session.get("usuario", "Sistema"), producto["nombre"], cantidad, total)

    # ✅ Actualizar stock
    conn.execute("UPDATE productos SET cantidad = cantidad - ? WHERE nombre=?", (cantidad, nombre))
    conn.commit()
    conn.close()
    return True

# --- PEDIDOS ---
def generar_pedidos():
    pedidos = {}
    for p in obtener_productos():
        faltante = p["stock_minimo"] - p["cantidad"]
        if faltante > 0:
            prov = p["proveedor"]
            if prov not in pedidos:
                pedidos[prov] = []
            pedidos[prov].append({
                "faltante": faltante,
                "nombre": p["nombre"],        # 🔹 ya no enviamos 'cantidad' ni 'stock_minimo'
                "precio_costo": p["precio_costo"]
            })
    return pedidos

def generar_mensaje_whatsapp(proveedor, lista_productos):
    mensaje = f"📦 *Pedido para {proveedor}* 📦\n\n"
    for p in lista_productos:
        # ✅ Formato: "<faltante> <nombre del producto>"
        mensaje += f"- {p['faltante']} {p['nombre']}\n"
    return urllib.parse.quote(mensaje)

# --- RUTAS WEB ---
@app.route("/")
def index():
    if "usuario" not in session:
        return redirect(url_for("login"))
    return lista_precios()

@app.route("/inventario")
def inventario():
    if "usuario" not in session:
        return redirect(url_for("login"))
    q = request.args.get("q", "").lower()
    productos_db = obtener_productos()
    productos = []

    for p in productos_db:
        precio_venta = p["precio_costo"] * (1 + (p["ganancia"] or 0) / 100)
        productos.append({
            "id": p["id"],
            "nombre": p["nombre"],
            "cantidad": p["cantidad"],
            "stock_minimo": p["stock_minimo"],
            "precio_costo": p["precio_costo"],
            "ganancia": p["ganancia"] or 0,
            "precio_venta": precio_venta,
            "proveedor": p["proveedor"]
        })

    # ✅ Filtro de búsqueda
    if q:
        productos = [p for p in productos if q in p["nombre"].lower() or q in p["proveedor"].lower()]

    # ✅ Calcular totales
    total_costo = sum(p["cantidad"] * p["precio_costo"] for p in productos)
    total_venta = sum(p["cantidad"] * p["precio_venta"] for p in productos)
    ganancia = total_venta - total_costo

    # ✅ Ahora sí se pasan al template
    return render_template("index.html",
                           productos=productos,
                           query=q,
                           total_costo=total_costo,
                           total_venta=total_venta,
                           ganancia=ganancia)

@app.route("/agregar", methods=["GET","POST"])
def agregar():
    if "usuario" not in session:
        return redirect(url_for("login"))
    
    if request.method == "POST":
        agregar_producto(
            request.form["nombre"],
            int(request.form["cantidad"]),
            int(request.form["stock_minimo"]),
            normalizar_precio(request.form["precio_costo"]),
            request.form["proveedor"],
            float(request.form["ganancia"].replace(',','.'))
        )
        return redirect(url_for("inventario"))
    return render_template("agregar.html")

@app.route("/editar/<int:id>", methods=["GET","POST"])
def editar(id):
    if "usuario" not in session:
        return redirect(url_for("login"))
    
    producto = obtener_producto(id)
    if not producto:
        return "No encontrado", 404
    
    if request.method == "POST":
        actualizar_producto(
            id,
            request.form["nombre"],
            int(request.form["cantidad"]),
            int(request.form["stock_minimo"]),
            normalizar_precio(request.form["precio_costo"]),
            request.form["proveedor"],
            float(request.form["ganancia"].replace(',','.'))
        )
        return redirect(url_for("inventario"))
    
    return render_template("editar.html", p=producto)

@app.route("/eliminar/<int:id>")
def eliminar(id):
    if "usuario" not in session:
        return redirect(url_for("login"))
    eliminar_producto(id)
    return redirect(url_for("inventario"))

@app.route("/pedidos")
def pedidos():
    if "usuario" not in session:
        return redirect(url_for("login"))

    pedidos = generar_pedidos()
    enlaces = {}
    totales = {}      # 🔹 Nuevo diccionario para totales por proveedor
    total_general = 0 # 🔹 Total general de todos los pedidos

    for prov, lista in pedidos.items():
        enlaces[prov] = f"https://wa.me/?text={generar_mensaje_whatsapp(prov, lista)}"
        total_prov = sum(p["faltante"] * p["precio_costo"] for p in lista)
        totales[prov] = total_prov
        total_general += total_prov

    return render_template("pedidos.html",
                           pedidos=pedidos,
                           enlaces=enlaces,
                           totales=totales,
                           total_general=total_general)


# --- LISTA DE PRECIOS ---
@app.route("/lista_precios")
def lista_precios():
    if "usuario" not in session:
        return redirect(url_for("login"))
    q = request.args.get("q", "").lower()
    productos = obtener_productos()
    lista = []

    for p in productos:
        precio_venta = p["precio_costo"] * (1 + (p["ganancia"] or 0) / 100)
        lista.append({
            "nombre": p["nombre"],
            "cantidad": p["cantidad"],  # ✅ agregamos la cantidad
            "precio_venta": precio_venta
        })

    # ✅ Filtrar búsqueda
    if q:
        lista = [p for p in lista if q in p["nombre"].lower()]

    return render_template("lista_precios.html", lista=lista, query=q)

@app.route("/vender/<nombre>/<int:cantidad>")
def vender(nombre, cantidad):
    if "usuario" not in session:
        return redirect(url_for("login"))

    resultado = registrar_venta_por_nombre(nombre, cantidad)

    if resultado is False:
        flash(f"❌ El producto '{nombre}' no existe.", "danger")
    elif resultado is None:
        flash(f"⚠️ No hay stock suficiente para vender {cantidad} unidades de {nombre}.", "warning")
    else:
        flash(f"✅ Se vendieron {cantidad} unidades de {nombre}.", "success")

    return redirect(url_for("lista_precios"))

@app.route("/exportar_precios")
def exportar_precios():
    if "usuario" not in session:
        return redirect(url_for("login"))
    productos = obtener_productos()
    data = []
    for p in productos:
        precio_venta = p["precio_costo"] * (1 + (p["ganancia"] or 0)/100)
        data.append({
            "Nombre": p["nombre"],
            "Precio Costo": p["precio_costo"],
            "% Ganancia": p["ganancia"] or 0,
            "Precio Venta": precio_venta,
            "Proveedor": p["proveedor"]
        })
    df = pd.DataFrame(data)
    file_name = "lista_precios.xlsx"
    df.to_excel(file_name, index=False)
    return send_file(file_name, as_attachment=True)

# --- BACKUP ---
from zipfile import ZipFile

@app.route("/backup")
def backup():
    if "usuario" not in session:
        return redirect(url_for("login"))
    
    fecha = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_zip = f"backup_{fecha}.zip"

    with ZipFile(backup_zip, "w") as zipf:
        if os.path.exists(DB_NAME):
            zipf.write(DB_NAME)
        if os.path.exists(LOG_DB):  # ✅ Agregar logs.db al backup
            zipf.write(LOG_DB)

    return send_file(backup_zip, as_attachment=True)

# --- IMPORTAR EXCEL ---
@app.route("/importar", methods=["GET", "POST"])
def importar_excel():
    if "usuario" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        file = request.files.get("archivo")
        if not file or not file.filename.endswith(".xlsx"):
            flash("❌ Debes subir un archivo Excel válido (.xlsx).", "danger")
            return redirect(url_for("importar_excel"))

        df = pd.read_excel(file)
        df.columns = df.columns.str.strip()

        # Columnas posibles
        columnas_validas = ["Nombre", "Precio Costo", "% Ganancia", "Cantidad", "Stock Mínimo", "Proveedor"]

        # Verificar que al menos exista la columna Nombre
        if "Nombre" not in df.columns:
            flash("❌ El Excel debe contener al menos la columna 'Nombre'.", "danger")
            return redirect(url_for("importar_excel"))

        conn = get_connection()
        cur = conn.cursor()
        filas_ok, filas_err = 0, 0

        for _, row in df.iterrows():
            try:
                nombre = str(row["Nombre"]).strip()

                # Obtener valores solo si la columna está presente
                cantidad = int(row["Cantidad"]) if "Cantidad" in df.columns and not pd.isna(row["Cantidad"]) else None
                stock_minimo = int(row["Stock Mínimo"]) if "Stock Mínimo" in df.columns and not pd.isna(row["Stock Mínimo"]) else None
                precio_costo = float(str(row["Precio Costo"]).replace(",", ".")) if "Precio Costo" in df.columns and not pd.isna(row["Precio Costo"]) else None
                ganancia = float(str(row["% Ganancia"]).replace(",", ".")) if "% Ganancia" in df.columns and not pd.isna(row["% Ganancia"]) else None
                proveedor = str(row["Proveedor"]).strip() if "Proveedor" in df.columns and not pd.isna(row["Proveedor"]) else None

                # Verificar si el producto ya existe
                cur.execute("SELECT id FROM productos WHERE nombre=?", (nombre,))
                existe = cur.fetchone()

                if existe:
                    # Generar dinámicamente la sentencia UPDATE solo con las columnas presentes
                    campos = []
                    valores = []
                    if cantidad is not None:
                        campos.append("cantidad=?")
                        valores.append(cantidad)
                    if stock_minimo is not None:
                        campos.append("stock_minimo=?")
                        valores.append(stock_minimo)
                    if precio_costo is not None:
                        campos.append("precio_costo=?")
                        valores.append(precio_costo)
                    if proveedor is not None:
                        campos.append("proveedor=?")
                        valores.append(proveedor)
                    if ganancia is not None:
                        campos.append("ganancia=?")
                        valores.append(ganancia)

                    if campos:
                        valores.append(nombre)
                        sql = f"UPDATE productos SET {', '.join(campos)} WHERE nombre=?"
                        cur.execute(sql, tuple(valores))
                else:
                    # Si no existe, insertar usando valores disponibles o por defecto
                    cur.execute("""
                        INSERT INTO productos (nombre, cantidad, stock_minimo, precio_costo, proveedor, ganancia)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        nombre,
                        cantidad if cantidad is not None else 0,
                        stock_minimo if stock_minimo is not None else 0,
                        precio_costo if precio_costo is not None else 0,
                        proveedor if proveedor is not None else "",
                        ganancia if ganancia is not None else 0
                    ))
                filas_ok += 1
            except Exception as e:
                filas_err += 1
                print(f"❌ Error al procesar {row.to_dict()}: {e}")

        conn.commit()
        conn.close()

        flash(f"✅ Importación completada: {filas_ok} productos actualizados/insertados, {filas_err} errores.", "success")
        return redirect(url_for("index"))

    return render_template("importar.html")


# --- EXPORTAR EXCEL GENERAL ---
@app.route("/exportar")
def exportar_excel():
    if "usuario" not in session:
        return redirect(url_for("login"))

    productos = obtener_productos()
    data = []

    # ✅ Construir el formato exacto para exportar
    for p in productos:
        data.append({
            "Nombre": p["nombre"],
            "Precio Costo": p["precio_costo"],
            "% Ganancia": p["ganancia"] or 0,
            "Cantidad": p["cantidad"],
            "Stock Mínimo": p["stock_minimo"],
            "Proveedor": p["proveedor"]
        })

    # ✅ Orden de columnas igual al esperado en la importación
    df = pd.DataFrame(data, columns=["Nombre", "Precio Costo", "% Ganancia", "Cantidad", "Stock Mínimo", "Proveedor"])

    # ✅ Nombre del archivo con fecha (opcional)
    from datetime import datetime
    file_name = f"productos_export_{datetime.now().strftime('%Y%m%d')}.xlsx"

    # ✅ Exportar a Excel
    df.to_excel(file_name, index=False)
    return send_file(file_name, as_attachment=True)


# --- RUTA DE LOGIN ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form["usuario"]
        password = request.form["password"]

        conn = get_connection()
        user = conn.execute("SELECT * FROM usuarios WHERE usuario=?", (usuario,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["usuario"] = usuario
            flash(f"✅ Bienvenido, {usuario}", "success")
            return redirect(url_for("lista_precios"))
        else:
            flash("❌ Usuario o contraseña incorrectos", "danger")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("✅ Sesión cerrada", "info")
    return redirect(url_for("login"))

@app.route("/cambiar_password", methods=["GET", "POST"])
def cambiar_password():
    if "usuario" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        actual = request.form["actual"]
        nueva = request.form["nueva"]
        confirmar = request.form["confirmar"]

        if nueva != confirmar:
            flash("❌ Las contraseñas nuevas no coinciden", "danger")
            return redirect(url_for("cambiar_password"))

        conn = get_connection()
        cur = conn.cursor()
        user = cur.execute("SELECT * FROM usuarios WHERE usuario=?", (session["usuario"],)).fetchone()

        from werkzeug.security import check_password_hash, generate_password_hash
        if not user or not check_password_hash(user["password"], actual):
            flash("❌ Contraseña actual incorrecta", "danger")
            conn.close()
            return redirect(url_for("cambiar_password"))

        cur.execute("UPDATE usuarios SET password=? WHERE usuario=?",
                    (generate_password_hash(nueva), session["usuario"]))
        conn.commit()
        conn.close()
        flash("✅ Contraseña cambiada con éxito", "success")
        return redirect(url_for("inventario"))

    return render_template("cambiar_password.html")

# --- RUTA PARA LOGS ---

@app.route("/logs")
def ver_logs():
    if "usuario" not in session:
        return redirect(url_for("login"))

    desde = request.args.get("desde")
    hasta = request.args.get("hasta")

    conn = get_connection_logs()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 🔹 Consulta filtrada
    query = "SELECT * FROM logs WHERE 1=1"
    params = []

    if desde:
        query += " AND fecha >= ?"
        params.append(desde + " 00:00:00")
    if hasta:
        query += " AND fecha <= ?"
        params.append(hasta + " 23:59:59")

    query += " ORDER BY fecha DESC"
    logs = cur.execute(query, params).fetchall()

    # 🔹 Calcular total solo para registros de ventas
    total_ventas = 0
    for log in logs:
        if log["accion"] == "VENTA":
            # Extraer el número del detalle (formato: "Venta de X x Producto - Total $YYY")
            import re
            match = re.search(r"Total \$([\d.,]+)", log["detalle"])
            if match:
                total_ventas += float(match.group(1).replace(",", "."))

    conn.close()

    return render_template("logs.html", logs=logs, total_ventas=total_ventas)

# --- MAIN ---
if __name__ == "__main__":
    init_db()
    init_logs_table()   # ✅ Inicializa la tabla logs
    app.run(debug=True)
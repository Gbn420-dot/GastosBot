"""GastosBot — app.py generado.
ARCHIVO GENERADO por tools/build_single.py (fuente 5990a506e4be). NO EDITAR A MANO."""
import base64
import csv
import hashlib
import hmac
import io
import logging
import math
import os
import random
import re
import time
import unicodedata
import warnings
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Flask, Response, abort, jsonify, request, session
from sqlalchemy import CheckConstraint, Column, Date, DateTime, Index, Integer, Numeric, String, create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ======== módulo: config ========
"""Configuración desde variables de entorno (validada al arrancar)."""





class ConfigError(RuntimeError):
    pass


def load_config(env=None):
    env = os.environ if env is None else env
    prod = env.get("APP_ENV", "").lower() == "production" or bool(env.get("RENDER"))
    pin, secret, url = env.get("APP_PIN"), env.get("SECRET_KEY"), env.get("DATABASE_URL")

    if prod:
        faltan = [n for n, v in (("APP_PIN", pin), ("SECRET_KEY", secret), ("DATABASE_URL", url)) if not v]
        if faltan:
            raise ConfigError("Faltan variables de entorno en producción: " + ", ".join(faltan))
        if len(secret) < 16:
            raise ConfigError("SECRET_KEY debe tener al menos 16 caracteres")
    else:  # desarrollo local
        pin = pin or "1234"
        secret = secret or "solo-desarrollo-local"
        url = url or "sqlite:///local_test.db"

    if not re.fullmatch(r"\d{4,6}", str(pin)):
        raise ConfigError("APP_PIN debe ser numérico de 4 a 6 dígitos")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    tz = env.get("APP_TZ", "America/Caracas")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"APP_TZ inválida: {tz}")
    return {"prod": prod, "pin": str(pin), "secret_key": secret, "database_url": url, "tz": tz}


# ======== módulo: clock ========
"""Reloj en la zona horaria del cliente (Render corre en UTC)."""




def make_clock(tz):
    zona = ZoneInfo(tz)
    return lambda: datetime.now(zona).replace(tzinfo=None)


# ======== módulo: models ========
Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Configuracion(Base):
    __tablename__ = "configuracion"
    id = Column(Integer, primary_key=True)
    nombre_usuario = Column(String(100), nullable=True)
    saldo_inicial_usd = Column(Numeric(16, 2), nullable=False, default=0)


class TasaCambio(Base):
    __tablename__ = "tasas_cambio"
    __table_args__ = (CheckConstraint("valor > 0", name="ck_tasa_positiva"),)
    id = Column(Integer, primary_key=True)
    fecha = Column(Date, unique=True, nullable=False)
    valor = Column(Numeric(14, 4), nullable=False)


class Movimiento(Base):
    """Gasto o ingreso. Tasa y USD quedan congelados al registrar."""
    __tablename__ = "movimientos"
    __table_args__ = (
        CheckConstraint("tipo IN ('gasto','ingreso')", name="ck_mov_tipo"),
        CheckConstraint("monto_bs > 0", name="ck_mov_monto"),
        Index("ix_mov_fecha_tipo", "fecha", "tipo"),
    )
    id = Column(Integer, primary_key=True)
    tipo = Column(String(10), nullable=False, default="gasto")
    categoria = Column(String(100), nullable=False)
    descripcion = Column(String(200), nullable=True)
    monto_bs = Column(Numeric(16, 2), nullable=False)
    tasa_aplicada = Column(Numeric(14, 4), nullable=False)
    monto_usd = Column(Numeric(16, 2), nullable=False)
    fecha = Column(Date, nullable=False)
    creado_en = Column(DateTime, default=_utcnow)


class Presupuesto(Base):
    __tablename__ = "presupuestos"
    __table_args__ = (CheckConstraint("limite_usd > 0", name="ck_pres_limite"),)
    id = Column(Integer, primary_key=True)
    categoria = Column(String(100), nullable=False, unique=True)
    limite_usd = Column(Numeric(16, 2), nullable=False)


class IntentoAuth(Base):
    """Contadores anti fuerza bruta (compartidos entre workers vía BD)."""
    __tablename__ = "intentos_auth"
    clave = Column(String(64), primary_key=True)  # IP o '*' (global)
    fallos = Column(Integer, nullable=False, default=0)
    bloqueado_hasta = Column(DateTime, nullable=True)
    actualizado = Column(DateTime, nullable=True)


# ======== módulo: database ========
database_log = logging.getLogger("gastosbot.db")


class Database:
    def __init__(self, url):
        kw = {"pool_pre_ping": True, "pool_recycle": 300}
        if url.startswith("postgresql"):
            args = {"connect_timeout": 10}
            if "sslmode" not in url:
                args["sslmode"] = "require"
            kw.update(connect_args=args, pool_size=3, max_overflow=2)
        else:
            warnings.filterwarnings("ignore", message=".*Decimal objects natively.*")
        self.engine = create_engine(url, **kw)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.ready = False

    def init(self, retries=5, base_delay=1.0, sleep=time.sleep):
        """Crea tablas con reintentos (Neon serverless puede tardar en despertar).
        El lock consultivo evita carreras entre workers de Gunicorn."""
        for i in range(retries):
            try:
                with self.engine.begin() as conn:
                    if conn.dialect.name == "postgresql":
                        conn.execute(text("SELECT pg_advisory_xact_lock(727274)"))
                    Base.metadata.create_all(conn)
                self.ready = True
                return
            except SQLAlchemyError as e:
                database_log.warning("init BD intento %d/%d falló: %s", i + 1, retries, e.__class__.__name__)
                if i == retries - 1:
                    raise
                sleep(base_delay * 2 ** i)

    def ensure_ready(self):
        if self.ready:
            return True
        try:
            self.init(retries=1)
            return True
        except SQLAlchemyError:
            return False

    @contextmanager
    def session(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()


# ======== módulo: security ========
"""PIN en tiempo constante + bloqueo por intentos (por IP y global)."""




VENTANA_MIN = 30


def _reglas(ip):  # (clave, máx. fallos, minutos de bloqueo)
    return ((ip[:64], 5, 5), ("*", 20, 15))


def pin_valido(ingresado, real):
    return hmac.compare_digest(str(ingresado or "").encode(), str(real).encode())


def segundos_bloqueo(s, ip, ahora):
    mayor = 0
    for clave, _, _ in _reglas(ip):
        r = s.get(IntentoAuth, clave)
        if r and r.bloqueado_hasta and r.bloqueado_hasta > ahora:
            mayor = max(mayor, int((r.bloqueado_hasta - ahora).total_seconds()) + 1)
    return mayor


def registrar_fallo(s, ip, ahora):
    for clave, maximo, minutos in _reglas(ip):
        r = s.get(IntentoAuth, clave)
        if r is None:
            r = IntentoAuth(clave=clave, fallos=0)
            s.add(r)
        vencido = r.bloqueado_hasta and r.bloqueado_hasta <= ahora
        antiguo = r.actualizado and ahora - r.actualizado > timedelta(minutes=VENTANA_MIN)
        if vencido or antiguo:
            r.fallos, r.bloqueado_hasta = 0, None
        r.fallos += 1
        r.actualizado = ahora
        if r.fallos >= maximo:
            r.bloqueado_hasta = ahora + timedelta(minutes=minutos)


def limpiar(s, ip):
    r = s.get(IntentoAuth, ip[:64])
    if r:
        s.delete(r)


# ======== módulo: parser ========
"""Intérprete de mensajes (puro, sin BD). interpretar(texto, hoy) -> (tipo, datos)."""






parser_MESES = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
         "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
DIAS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
FILLER = {"gaste", "pague", "compre", "en", "de", "del", "el", "la", "por", "un", "una", "para", "mi", "mis"}
USD_WORDS = {"usd", "$", "us$", "dolar", "dolares"}
BS_WORDS = {"bs", "bss", "bolivar", "bolivares"}
ALIAS = {"super": "Supermercado", "supermercado": "Supermercado"}
KW_REPORTE = {"reporte", "resumen", "informe"}
RE_NUM = re.compile(r"^\$?\d[\d.,]*\$?$")
RE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
RE_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?$|^(\d{1,2})-(\d{1,2})-(\d{4}|\d{2})$")
MSG_FECHA = "Esa fecha no existe. Usa DD/MM/AAAA, AAAA-MM-DD o palabras como 'ayer'."


def norm(t):
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def canon_categoria(palabra):
    p = palabra.strip()
    return ALIAS.get(norm(p)) or (p[:1].upper() + p[1:].lower())


def parse_monto(s):
    """'1.234,50' '1,234.50' '1.500'(=1500) '12.5' '0,5' -> Decimal; None si inválido."""
    s = s.strip("$")
    if not re.fullmatch(r"\d[\d.,]*", s) or s[-1] in ".,":
        return None
    p, c = "." in s, "," in s
    if p and c:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        mil = "," if dec == "." else "."
        if s.count(dec) != 1:
            return None
        ent, frac = s.split(dec)
        if not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(mil)}\d{{3}})*", ent):
            return None
        txt = ent.replace(mil, "") + "." + frac
    elif p or c:
        partes = s.split("." if p else ",")
        if len(partes) > 2:
            if not (1 <= len(partes[0]) <= 3 and all(len(x) == 3 for x in partes[1:])):
                return None
            txt = "".join(partes)
        else:
            ent, frac = partes
            miles = len(frac) == 3 and 1 <= len(ent) <= 3 and not ent.startswith("0")
            txt = ent + frac if miles else ent + "." + frac
    else:
        txt = s
    try:
        return Decimal(txt)
    except InvalidOperation:
        return None


def _tokens(texto):
    out = []
    for raw in texto.split():
        raw = raw.strip(",;:!?¡¿()\"").rstrip(".")
        if raw:
            out.append((raw, norm(raw)))
    return out


def _es_num(n):
    return bool(RE_NUM.match(n))


def _fecha_numerica(n, hoy):
    m = RE_ISO.match(n)
    if m:
        return date(*map(int, m.groups()))
    m = RE_DMY.match(n)
    if m:
        d, mo, y = m.groups()[:3] if m.group(1) else m.groups()[3:]
        y = int(y) if y else hoy.year
        return date(y + 2000 if y < 100 else y, int(mo), int(d))
    return None


def _extraer_fecha(toks, hoy):
    """Busca la primera expresión de fecha. Devuelve (fecha|None, tokens sin ella).
    Lanza ValueError si la fecha no existe."""
    n = [t[1] for t in toks]
    for i, x in enumerate(n):
        f, largo = None, 0
        if x in ("ayer", "anteayer", "antier", "hoy"):
            f, largo = hoy - timedelta(days={"ayer": 1, "anteayer": 2, "antier": 2, "hoy": 0}[x]), 1
        elif x in DIAS:
            idx = DIAS[x]
            if n[i + 1:i + 4] == ["de", "la", "semana"] and n[i + 4:i + 5] in (["pasada"], ["anterior"]):
                lunes_actual = hoy - timedelta(days=hoy.weekday())
                f, largo = lunes_actual - timedelta(days=7) + timedelta(days=idx), 5
            elif n[i + 1:i + 2] in (["pasado"], ["pasada"]):
                f, largo = hoy - timedelta(days=(hoy.weekday() - idx) % 7 or 7), 2
            else:
                f, largo = hoy - timedelta(days=(hoy.weekday() - idx) % 7), 1
        elif re.fullmatch(r"\d{1,2}", x) and n[i + 1:i + 2] == ["de"] and n[i + 2:i + 3] and n[i + 2] in parser_MESES:
            anio, largo = hoy.year, 3
            if n[i + 3:i + 4] == ["de"] and n[i + 4:i + 5] and re.fullmatch(r"\d{4}", n[i + 4]):
                anio, largo = int(n[i + 4]), 5
            f = date(anio, parser_MESES[n[i + 2]], int(x))
        else:
            f = _fecha_numerica(x, hoy)
            largo = 1 if f else 0
        if f:
            ini = i - 1 if i > 0 and n[i - 1] == "el" else i
            return f, toks[:ini] + toks[i + largo:]
    return None, toks


def _parser_error(msg):
    return "error", {"msg": msg}


def _periodo_reporte(toks, hoy):
    n = [t[1] for t in toks]
    nums = [x for x in n if _es_num(x) and not re.fullmatch(r"20\d\d", x)]
    mes = next((parser_MESES[x] for x in n if x in parser_MESES), None)
    anio = next((int(x) for x in n if re.fullmatch(r"20\d\d", x)), None)
    frase = any(n[i:i + 2] in (["cuanto", "gaste"], ["gastos", "de"]) for i in range(len(n)))
    kw = any(x in KW_REPORTE for x in n) or frase
    ruido = {"de", "del", "en", "mes", "el", "ano", "anual"}
    solo = bool(mes) and all(x in parser_MESES or x in ruido or re.fullmatch(r"20\d\d", x) for x in n)
    if nums or not (kw or solo):
        return None
    if mes is None and (anio or "ano" in n or "anual" in n):
        return {"mes": None, "anio": anio or hoy.year}
    if mes is None:
        return {"mes": hoy.month, "anio": hoy.year}
    return {"mes": mes, "anio": anio or hoy.year}


def _parser_movimiento(tipo, toks, hoy):
    fecha, toks = _extraer_fecha(toks, hoy)
    usd, nums, palabras = False, [], []
    for o, x in toks:
        if x in USD_WORDS:
            usd = True
        elif x in BS_WORDS:
            continue
        elif _es_num(x):
            usd = usd or x.startswith("$") or x.endswith("$")
            nums.append(o)
        else:
            palabras.append(o)
    if not nums:
        return _parser_error("No entendí el monto. Prueba: Gasolina 500 o Comida ayer 350")
    if len(nums) > 1:
        return _parser_error("Veo más de un monto. Escribe uno solo, ej: Comida 350 almuerzo")
    monto = parse_monto(nums[0])
    if monto is None:
        return _parser_error("No pude leer el monto. Usa formatos como 1500, 1.500,50 o 12.5")
    while palabras and norm(palabras[0]) in FILLER:
        palabras.pop(0)
    cat = canon_categoria(palabras[0])[:100] if palabras else ("Ingreso" if tipo == "ingreso" else "General")
    desc = " ".join(palabras[1:])[:200] or None
    return "movimiento", {"tipo": tipo, "categoria": cat, "descripcion": desc, "monto": monto,
                          "moneda": "usd" if usd else "bs", "fecha": fecha or hoy}


def interpretar(texto, hoy):
    try:
        return _interpretar(texto, hoy)
    except ValueError:
        return _parser_error(MSG_FECHA)


def _interpretar(texto, hoy):
    toks = _tokens(texto)
    if not toks:
        return _parser_error("Escribe un gasto o pide un reporte.")
    n0 = toks[0][1]
    if n0.startswith("/") or n0 in ("exportar", "ayuda", "deshacer"):
        cmd = {"/exportar": "exportar", "exportar": "exportar", "/ayuda": "ayuda", "ayuda": "ayuda",
               "/help": "ayuda", "/deshacer": "deshacer", "deshacer": "deshacer"}.get(n0)
        return (cmd, {}) if cmd else _parser_error("Comando desconocido. Escribe /ayuda.")
    if n0 == "tasa":
        fecha, rest = _extraer_fecha(toks[1:], hoy)
        nums = [t[1] for t in rest if _es_num(t[1])]
        if len(nums) != 1 or parse_monto(nums[0]) is None:
            return _parser_error("Escribe la tasa así: tasa 915 (o tasa 915 15/11/2026)")
        return "tasa", {"valor": parse_monto(nums[0]), "fecha": fecha or hoy}
    if n0 == "saldo":
        if len(toks) == 1:
            return "saldo", {}
        if len(toks) == 3 and toks[1][1] == "inicial" and parse_monto(toks[2][1]) is not None:
            return "saldo_inicial", {"valor": parse_monto(toks[2][1])}
        return _parser_error("Usa: saldo  o  saldo inicial 1000 (en USD)")
    if n0 == "presupuesto":
        pal = [o for o, x in toks[1:] if not _es_num(x) and x not in USD_WORDS]
        nums = [x for _, x in toks[1:] if _es_num(x)]
        if not pal or len(nums) != 1 or parse_monto(nums[0]) is None:
            return _parser_error("Usa: presupuesto super 200 (USD al mes; 0 para quitarlo)")
        return "presupuesto", {"categoria": canon_categoria(pal[0]), "limite": parse_monto(nums[0])}
    rep = _periodo_reporte(toks, hoy)
    if rep:
        return "reporte", rep
    tipo = "gasto"
    if n0 in ("ingreso", "ingresos"):
        tipo, toks = "ingreso", toks[1:]
    return _parser_movimiento(tipo, toks, hoy)


def generar_saludo(nombre, hora):
    h = "Buenos días" if 5 <= hora < 12 else ("Buenas tardes" if 12 <= hora < 19 else "Buenas noches")
    return random.choice([f"{h}, {nombre}. ¿En qué gastamos hoy?",
                          f"Hola {nombre}, ¿cómo va el día? Listo para anotar.",
                          f"¿Ya tomaste café, {nombre}? Cuéntame tus movimientos."])


# ======== módulo: services ========
"""Consultas y agregaciones. Todo el dinero se maneja en Decimal."""




Q2 = Decimal("0.01")
MAX_MONTO = Decimal("1000000000")
services_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def d2(x):
    return Decimal(x).quantize(Q2, rounding=ROUND_HALF_UP)


def f2(x):
    return float(d2(x))


def tasa_para_fecha(s, fecha):
    r = s.query(TasaCambio).filter(TasaCambio.fecha <= fecha).order_by(TasaCambio.fecha.desc()).first()
    return Decimal(r.valor) if r else None


def rango(mes, anio):
    inicio = date(anio, mes or 1, 1)
    fin = date(anio + 1, 1, 1) if (mes is None or mes == 12) else date(anio, mes + 1, 1)
    return inicio, fin


def movimientos(s, tipo, inicio, fin):
    return (s.query(Movimiento)
            .filter(Movimiento.tipo == tipo, Movimiento.fecha >= inicio, Movimiento.fecha < fin)
            .order_by(Movimiento.fecha.desc(), Movimiento.id.desc()).all())


def generar_reporte(s, mes, anio):
    gastos = movimientos(s, "gasto", *rango(mes, anio))
    if not gastos:
        return None
    cats = {}
    for g in gastos:
        c = cats.setdefault(g.categoria, {"total_bs": Decimal(0), "total_usd": Decimal(0), "items": []})
        c["total_bs"] += g.monto_bs
        c["total_usd"] += g.monto_usd
        c["items"].append({"id": g.id, "descripcion": g.descripcion, "fecha": g.fecha.strftime("%d/%m/%Y"),
                           "monto_bs": f2(g.monto_bs), "monto_usd": f2(g.monto_usd),
                           "tasa": float(g.tasa_aplicada)})
    total_bs = sum((g.monto_bs for g in gastos), Decimal(0))
    total_usd = sum((g.monto_usd for g in gastos), Decimal(0))
    orden = sorted(cats.items(), key=lambda kv: kv[1]["total_bs"], reverse=True)
    return {
        "titulo": f"Reporte de {services_MESES[mes - 1].capitalize()} {anio}" if mes else f"Resumen Anual {anio}",
        "total_bs": f2(total_bs), "total_usd": f2(total_usd),
        "tasa_promedio": f2(total_bs / total_usd) if total_usd else None,
        "categorias": {k: {**v, "total_bs": f2(v["total_bs"]), "total_usd": f2(v["total_usd"])} for k, v in orden},
    }


def services_resumen(s, hoy):
    """Datos de la pantalla principal: saldo, ahorro del mes y presupuestos."""
    cfg = s.query(Configuracion).first()
    base = Decimal(cfg.saldo_inicial_usd or 0) if cfg else Decimal(0)
    todos = s.query(Movimiento).all()
    ing = sum((m.monto_usd for m in todos if m.tipo == "ingreso"), Decimal(0))
    gas = sum((m.monto_usd for m in todos if m.tipo == "gasto"), Decimal(0))
    ini, fin = rango(hoy.month, hoy.year)
    mes = [m for m in todos if ini <= m.fecha < fin]
    ing_m = sum((m.monto_usd for m in mes if m.tipo == "ingreso"), Decimal(0))
    gas_m = sum((m.monto_usd for m in mes if m.tipo == "gasto"), Decimal(0))
    pres = []
    for p in s.query(Presupuesto).order_by(Presupuesto.categoria).all():
        usado = sum((m.monto_usd for m in mes if m.tipo == "gasto"
                     and m.categoria.casefold() == p.categoria.casefold()), Decimal(0))
        pres.append({"categoria": p.categoria, "limite_usd": f2(p.limite_usd), "gastado_usd": f2(usado)})
    onboarding = bool(cfg and cfg.nombre_usuario and tasa_para_fecha(s, hoy) is not None)
    return {"onboarding_completo": onboarding, "saldo_usd": f2(base + ing - gas), "ingresos_mes_usd": f2(ing_m),
            "gastos_mes_usd": f2(gas_m), "ahorro_mes_usd": f2(ing_m - gas_m), "presupuestos": pres}


def tasa_para_fecha_o_primera(s, fecha):
    """(tasa, aproximada). Si no hay tasa <= fecha usa la primera registrada y lo marca."""
    t = tasa_para_fecha(s, fecha)
    if t is not None:
        return t, False
    r = s.query(TasaCambio).order_by(TasaCambio.fecha.asc()).first()
    return (Decimal(r.valor), True) if r else (None, False)


def _buscar_presupuesto(s, categoria):
    return next((p for p in s.query(Presupuesto).all() if p.categoria.casefold() == categoria.casefold()), None)


def fijar_presupuesto(s, categoria, limite):
    """limite 0 elimina. Devuelve 'creado' | 'actualizado' | 'eliminado' | 'nada'."""
    p = _buscar_presupuesto(s, categoria)
    if limite == 0:
        if p:
            s.delete(p)
            return "eliminado"
        return "nada"
    if p:
        p.limite_usd = d2(limite)
        return "actualizado"
    s.add(Presupuesto(categoria=categoria, limite_usd=d2(limite)))
    return "creado"


def estado_presupuesto(s, categoria, hoy):
    """(gastado_usd, limite_usd) del mes para la categoría, o None si no tiene presupuesto."""
    p = _buscar_presupuesto(s, categoria)
    if not p:
        return None
    ini, fin = rango(hoy.month, hoy.year)
    usado = sum((m.monto_usd for m in movimientos(s, "gasto", ini, fin)
                 if m.categoria.casefold() == categoria.casefold()), Decimal(0))
    return usado, Decimal(p.limite_usd)


# ======== módulo: conversacion ========
"""Lógica del chat: recibe el texto, aplica reglas de negocio y devuelve (payload, http_status)."""







AYUDA = ("Puedes escribir:\n"
         "• Gasolina 500 · Comida ayer 350 · Super $20\n"
         "• ingreso sueldo 900 (Bs; con $ o usd va en dólares)\n"
         "• tasa 915 · tasa 915 15/11/2026\n"
         "• reporte · septiembre 2025 · resumen del año 2026\n"
         "• presupuesto super 200 (USD/mes) · saldo · saldo inicial 1000\n"
         "• /exportar · /deshacer (borra el último movimiento)")


def _t(msg, code=200, **extra):
    return {"tipo": "texto", "respuesta": msg, **extra}, code


def procesar(s, texto, ts):
    hoy = ts.date()
    cfg = s.query(Configuracion).first()
    if not cfg or not cfg.nombre_usuario:
        nombre = re.sub(r"^(me llamo|mi nombre es|soy)\s+", "", texto, flags=re.I).strip()
        if not nombre or len(nombre) > 60 or re.search(r"\d|[<>/]", nombre):
            return _t("Dime solo tu nombre, por favor (sin números).")
        nombre = nombre.title()
        if cfg:
            cfg.nombre_usuario = nombre
        else:
            s.add(Configuracion(nombre_usuario=nombre))
        return _t(f"Un gusto, {nombre}. Ahora dime cuál es el valor de la tasa de cambio de hoy (ejemplo: tasa 915).")

    tasa_hoy = tasa_para_fecha(s, hoy)
    if tasa_hoy is None and re.fullmatch(r"[\d.,]+", texto):
        texto = "tasa " + texto  # onboarding: basta con el número
    kind, d = interpretar(texto, hoy)
    if tasa_hoy is None and kind not in ("tasa", "ayuda", "error"):
        return _t(f"{cfg.nombre_usuario}, primero necesito saber la tasa del día. Escribe por ejemplo: tasa 915")
    return HANDLERS[kind](s, d, ts, cfg)


def _conversacion_error(s, d, ts, cfg):
    return _t(d["msg"])


def _ayuda(s, d, ts, cfg):
    return _t(AYUDA)


def _exportar(s, d, ts, cfg):
    return _t("📥 Descargando el respaldo CSV del mes…", tipo="descarga", url="/api/exportar")


def _tasa(s, d, ts, cfg):
    v, f, hoy = d["valor"], d["fecha"], ts.date()
    if v <= 0 or v >= MAX_MONTO:
        return _t("La tasa debe ser un número mayor que cero.")
    if f > hoy:
        return _t("No puedo fijar la tasa de una fecha futura.")
    v = v.quantize(Decimal("0.0001"))
    reg = s.query(TasaCambio).filter(TasaCambio.fecha == f).first()
    if reg:
        reg.valor = v
    else:
        s.add(TasaCambio(fecha=f, valor=v))
    extra = "" if f == hoy else " Los gastos ya registrados conservan su tasa."
    return _t(f"✅ Tasa del {f.strftime('%d/%m/%Y')}: Bs. {v:,.2f}.{extra} Ya puedes registrar tus gastos.")


def _saldo_inicial(s, d, ts, cfg):
    if d["valor"] >= MAX_MONTO:
        return _t("Ese saldo es demasiado grande.")
    cfg.saldo_inicial_usd = d2(d["valor"])
    return _t(f"✅ Saldo inicial: $ {d2(d['valor']):,.2f}")


def _saldo(s, d, ts, cfg):
    r = services_resumen(s, ts.date())
    return _t(f"💼 Saldo disponible: **$ {r['saldo_usd']:,.2f}**\n"
              f"📈 Ahorro del mes: $ {r['ahorro_mes_usd']:,.2f}  (ingresos {r['ingresos_mes_usd']:,.2f} − gastos {r['gastos_mes_usd']:,.2f})",
              resumen=r)


def _presupuesto(s, d, ts, cfg):
    if d["limite"] >= MAX_MONTO:
        return _t("Ese límite es demasiado grande.")
    r = fijar_presupuesto(s, d["categoria"], d["limite"])
    if r == "eliminado":
        return _t(f"🗑️ Presupuesto de {d['categoria']} eliminado.")
    if r == "nada":
        return _t(f"{d['categoria']} no tenía presupuesto.")
    return _t(f"✅ Presupuesto de {d['categoria']}: $ {d2(d['limite']):,.2f} al mes.")


def _reporte(s, d, ts, cfg):
    rep = generar_reporte(s, d["mes"], d["anio"])
    if not rep:
        return _t("No encontré gastos registrados para ese período.")
    return {"tipo": "reporte", "reporte": rep}, 200


def _deshacer(s, d, ts, cfg):
    m = s.query(Movimiento).order_by(Movimiento.id.desc()).first()
    if not m:
        return _t("No hay movimientos para deshacer.")
    msg = f"↩️ Borré {m.tipo}: {m.categoria} · Bs. {m.monto_bs:,.2f} · {m.fecha.strftime('%d/%m/%Y')}"
    s.delete(m)
    return _t(msg)


def _conversacion_movimiento(s, d, ts, cfg):
    hoy, monto, fecha = ts.date(), d["monto"], d["fecha"]
    if monto <= 0 or monto >= MAX_MONTO:
        return _t("El monto debe ser mayor que cero y razonable.")
    if fecha > hoy:
        return _t("No registro movimientos con fecha futura.")
    tasa, aprox = tasa_para_fecha_o_primera(s, fecha)
    if tasa is None:
        return _t("Primero necesito la tasa. Escribe por ejemplo: tasa 915")
    if d["moneda"] == "usd":
        usd, bs = d2(monto), d2(monto * tasa)
    else:
        bs, usd = d2(monto), d2(monto / tasa)
    if bs <= 0:
        return _t("El monto es demasiado pequeño.")
    m = Movimiento(tipo=d["tipo"], categoria=d["categoria"], descripcion=d["descripcion"], monto_bs=bs,
                   tasa_aplicada=tasa, monto_usd=usd, fecha=fecha, creado_en=ts)
    s.add(m)
    s.flush()
    icono = "✅" if d["tipo"] == "gasto" else "💰"
    et = "registrado" if d["tipo"] == "gasto" else "ingreso registrado"
    desc = f" — {d['descripcion']}" if d["descripcion"] else ""
    lineas = [f"{icono} **{d['categoria']}** {et}{desc}", f"📅 Fecha: {fecha.strftime('%d/%m/%Y')}",
              f"💵 Bs. {bs:,.2f}  |  **$ {usd:,.2f}** (Tasa: {tasa:,.2f})"]
    if aprox:
        lineas.append(f"⚠️ No había tasa anterior al {fecha.strftime('%d/%m/%Y')}; usé la primera registrada. "
                      f"Para fijarla: tasa X {fecha.strftime('%d/%m/%Y')} y vuelve a anotarlo.")
    if d["tipo"] == "gasto":
        est = estado_presupuesto(s, d["categoria"], hoy)
        if est and est[1] > 0:
            usado, lim = est
            pct = int(usado / lim * 100)
            if pct >= 100:
                lineas.append(f"🚨 Superaste el presupuesto de {d['categoria']}: $ {usado:,.2f} / $ {lim:,.2f}")
            elif pct >= 80:
                lineas.append(f"⚠️ Vas al {pct}% del presupuesto de {d['categoria']}: $ {usado:,.2f} / $ {lim:,.2f}")
    mov = {"id": m.id, "tipo": m.tipo, "categoria": m.categoria, "descripcion": m.descripcion,
           "monto_bs": float(bs), "monto_usd": float(usd), "tasa": float(tasa),
           "fecha": fecha.isoformat(), "hora": ts.strftime("%H:%M")}
    return _t("\n".join(lineas), movimiento=mov)


HANDLERS = {"error": _conversacion_error, "ayuda": _ayuda, "exportar": _exportar, "tasa": _tasa, "saldo": _saldo,
            "saldo_inicial": _saldo_inicial, "presupuesto": _presupuesto, "reporte": _reporte,
            "deshacer": _deshacer, "movimiento": _conversacion_movimiento}


# ======== módulo: assets ========
"""Archivos de la interfaz (HTML, CSS, JS, service worker, manifest, íconos).
En desarrollo se leen de disco; en el archivo único generado (tools/build_single.py)
el bloque marcado se reemplaza por los mismos bytes incrustados."""



ASSETS = {
    'index.html': '<!DOCTYPE html>\n<html lang="es">\n<head>\n  <meta charset="UTF-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />\n  <meta name="theme-color" content="#0F172A" />\n  <meta name="apple-mobile-web-app-capable" content="yes" />\n  <meta name="mobile-web-app-capable" content="yes" />\n  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />\n  <meta name="apple-mobile-web-app-title" content="GastosBot" />\n  <link rel="manifest" href="/static/manifest.json" />\n  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png" />\n  <link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png" />\n  <link rel="stylesheet" href="/static/app.css" />\n  <title>GastosBot</title>\n</head>\n<body>\n  <!-- Bloqueo por PIN -->\n  <main id="lock" class="lock" aria-labelledby="lock-title">\n    <div class="glass lock-card">\n      <div class="lock-emoji" aria-hidden="true">🔒</div>\n      <h1 id="lock-title">GastosBot</h1>\n      <p class="muted">Ingresa tu PIN para continuar</p>\n      <input id="pin" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="off"\n             placeholder="••••" aria-label="PIN" />\n      <button id="pin-btn" class="btn" type="button">Entrar</button>\n      <p id="pin-error" class="error" role="alert" hidden></p>\n    </div>\n  </main>\n\n  <!-- App -->\n  <div id="app" class="app" hidden>\n    <header class="glass topbar">\n      <span class="spacer" aria-hidden="true"></span>\n      <h1 class="title">GastosBot</h1>\n      <button id="btn-reporte" class="icon-btn" type="button" aria-label="Reporte del mes" title="Reporte del mes">📊</button>\n    </header>\n\n    <section id="chat" class="chat" aria-live="polite" aria-label="Conversación">\n      <div id="resumen" class="resumen" hidden></div>\n    </section>\n\n    <footer class="bottom">\n      <div class="chips" id="chips" role="group" aria-label="Accesos rápidos">\n        <button class="chip" type="button" data-prefijo="Gasolina ">⛽ Gasolina</button>\n        <button class="chip" type="button" data-prefijo="Comida ">🍔 Comida</button>\n        <button class="chip" type="button" data-prefijo="Super ">🛒 Super</button>\n        <button class="chip" type="button" data-prefijo="Servicios ">💡 Servicios</button>\n        <button class="chip" type="button" data-prefijo="Salud ">💊 Salud</button>\n      </div>\n      <form id="form" class="glass composer" autocomplete="off">\n        <input id="texto" type="text" enterkeyhint="send" autocomplete="off" autocapitalize="sentences"\n               maxlength="300" placeholder="Escribe tu mensaje…" aria-label="Mensaje" />\n        <button id="enviar" class="send" type="submit" aria-label="Enviar">➤</button>\n      </form>\n    </footer>\n  </div>\n\n  <script src="/static/app.js"></script>\n</body>\n</html>\n'.encode('utf-8'),
    'static/app.css': ':root {\n  --mint: #34D399; --mint-soft: rgba(52, 211, 153, .18); --mint-line: rgba(52, 211, 153, .55);\n  --text: #F8FAFC; --muted: #94A3B8; --red: #F87171; --amber: #FBBF24;\n  --glass: rgba(255, 255, 255, .08); --glass-border: rgba(255, 255, 255, .16);\n  --app-h: 100dvh; --app-top: 0px;\n}\n* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent;\n    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; }\nhtml, body { height: 100%; overflow: hidden; }\nbody { position: fixed; inset: 0; width: 100%; color: var(--text);\n       background: linear-gradient(160deg, #0F172A 0%, #1E1B4B 100%) fixed; }\n[hidden] { display: none !important; }\nbutton { cursor: pointer; color: inherit; border: 0; background: none; font: inherit; }\n.muted { color: var(--muted); font-size: .9rem; }\n.error { color: var(--red); font-size: .9rem; margin-top: 10px; }\n\n.glass { background: var(--glass); border: 1px solid var(--glass-border);\n         -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);\n         box-shadow: 0 8px 28px rgba(0, 0, 0, .25), inset 0 1px 0 rgba(255, 255, 255, .08); }\n@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {\n  .glass { background: rgba(30, 41, 80, .85); }\n}\n\n/* PIN */\n.lock { position: fixed; inset: 0; display: grid; place-items: center; padding: 24px; }\n.lock-card { width: min(340px, 100%); border-radius: 28px; padding: 28px 24px; text-align: center; }\n.lock-card h1 { margin: 6px 0 4px; font-size: 1.5rem; }\n.lock-emoji { font-size: 2.2rem; }\n#pin { width: 100%; margin: 18px 0 12px; padding: 14px; text-align: center; letter-spacing: .5em; font-size: 1.4rem;\n       color: var(--text); background: rgba(255, 255, 255, .07); border: 1px solid var(--glass-border); border-radius: 16px; outline: none; }\n#pin:focus { border-color: var(--mint-line); }\n.btn { width: 100%; padding: 13px; border-radius: 16px; font-weight: 600; color: #052e22; background: var(--mint); }\n.btn:disabled { opacity: .5; }\n\n/* Layout */\n.app { position: fixed; left: 0; right: 0; top: var(--app-top); height: var(--app-h);\n       display: flex; flex-direction: column; max-width: 720px; margin: 0 auto; }\n.topbar { display: flex; align-items: center; justify-content: space-between; z-index: 5;\n          margin: calc(8px + env(safe-area-inset-top)) 12px 0; padding: 8px 10px; border-radius: 22px; }\n.title { font-size: 1.1rem; font-weight: 650; text-align: center; letter-spacing: .02em; }\n.spacer, .icon-btn { width: 40px; height: 36px; }\n.icon-btn { font-size: 1.3rem; border-radius: 12px; background: var(--mint-soft); border: 1px solid var(--mint-line); }\n.icon-btn:active { transform: scale(.94); }\n.chat { flex: 1; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch;\n        padding: 14px 12px 8px; display: flex; flex-direction: column; gap: 10px; }\n\n/* Resumen */\n.resumen { display: flex; flex-direction: column; gap: 10px; }\n.saldo { display: flex; align-items: center; gap: 16px; padding: 16px 18px; border-radius: 26px; }\n.ring { flex: none; width: 74px; height: 74px; }\n.ring circle { fill: none; stroke-width: 7; }\n.ring .track { stroke: rgba(255, 255, 255, .14); }\n.ring .prog { stroke: var(--mint); stroke-linecap: round; transform: rotate(-90deg); transform-origin: 50% 50%;\n              transition: stroke-dashoffset .6s ease; filter: drop-shadow(0 0 4px rgba(52, 211, 153, .6)); }\n.saldo .label { color: var(--muted); font-size: .85rem; }\n.saldo .monto { color: var(--mint); font-size: 1.9rem; font-weight: 700; line-height: 1.15; }\n.saldo .sub { font-size: .85rem; margin-top: 2px; }\n.presu { padding: 10px 14px; border-radius: 20px; }\n.presu .fila { display: flex; justify-content: space-between; gap: 8px; font-size: .92rem; }\n.presu .nombre { color: var(--mint); }\n.bar { height: 6px; margin-top: 7px; border-radius: 6px; background: rgba(255, 255, 255, .14); overflow: hidden; }\n.bar > i { display: block; height: 100%; border-radius: 6px; background: var(--mint); transition: width .5s ease; }\n.bar.warn > i { background: var(--amber); } .bar.over > i { background: var(--red); }\n\n/* Burbujas */\n.msg { max-width: 86%; padding: 10px 14px; border-radius: 20px; line-height: 1.38; white-space: pre-wrap;\n       overflow-wrap: anywhere; animation: pop .22s ease both; }\n.msg.user { align-self: flex-end; background: var(--mint-soft); border: 1px solid var(--mint-line); border-bottom-right-radius: 6px; }\n.msg.bot { align-self: flex-start; border-bottom-left-radius: 6px; }\n.msg.err { border-color: rgba(248, 113, 113, .5); }\n.msg strong { color: var(--mint); font-weight: 650; }\n.retry { display: block; margin-top: 8px; padding: 6px 14px; border-radius: 12px; background: var(--mint-soft); border: 1px solid var(--mint-line); }\n.mov { display: flex; gap: 12px; align-items: center; white-space: normal; min-width: 62%; }\n.mov .emoji { font-size: 1.6rem; }\n.mov .cuerpo { flex: 1; min-width: 0; }\n.mov .cat { color: var(--mint); font-size: 1.05rem; }\n.mov .desc, .mov .meta { color: var(--muted); font-size: .8rem; }\n.mov .der { text-align: right; }\n.mov .usd { font-weight: 650; } .mov .usd.pos { color: var(--mint); }\n.mov .nota { flex-basis: 100%; font-size: .82rem; color: var(--amber); }\n.mov-wrap { flex-wrap: wrap; }\n.typing span { display: inline-block; width: 7px; height: 7px; margin: 0 2px; border-radius: 50%; background: var(--muted); animation: blink 1.1s infinite; }\n.typing span:nth-child(2) { animation-delay: .18s; } .typing span:nth-child(3) { animation-delay: .36s; }\n\n/* Reporte */\n.reporte { align-self: stretch; display: flex; flex-direction: column; gap: 8px; animation: pop .25s ease both; }\n.rep-card { border-radius: 26px; padding: 14px 16px 16px; text-align: center; }\n.rep-card .rt { display: inline-block; padding: 5px 16px; border-radius: 12px; background: var(--glass); border: 1px solid var(--glass-border); font-size: .95rem; }\n.rep-card .tot { color: var(--mint); font-size: 1.55rem; font-weight: 700; margin-top: 8px; line-height: 1.25; }\n.rep-card .prom { color: var(--muted); font-size: .85rem; margin: 4px 0 8px; }\n.donut-row { display: flex; align-items: center; justify-content: center; gap: 16px; flex-wrap: wrap; }\n.donut { width: 130px; height: 130px; transform: rotate(-90deg); }\n.donut circle { fill: none; stroke-width: 6; }\n.leyenda { text-align: left; font-size: .8rem; display: grid; gap: 3px; }\n.leyenda i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }\n.pill { width: 100%; text-align: left; border-radius: 22px; padding: 0; background: rgba(52, 211, 153, .07); border: 1.5px solid var(--mint-line);\n        box-shadow: 0 0 14px rgba(52, 211, 153, .15); overflow: hidden; }\n.pill-head { display: flex; align-items: center; gap: 8px; width: 100%; padding: 12px 16px; }\n.pill-head .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.pill-head .chev { transition: transform .3s ease; color: var(--muted); }\n.pill.open { background: rgba(52, 211, 153, .14); }\n.pill.open .chev { transform: rotate(180deg); }\n.panel { display: grid; grid-template-rows: 0fr; transition: grid-template-rows .32s ease; }\n.pill.open .panel { grid-template-rows: 1fr; }\n.panel-inner { overflow: hidden; }\n.items { margin: 0 12px 12px; padding: 4px 12px; border-radius: 16px; background: rgba(15, 23, 42, .35); border: 1px solid var(--glass-border); }\n.item { display: flex; gap: 8px; padding: 8px 0; font-size: .88rem; border-bottom: 1px solid rgba(255, 255, 255, .08); }\n.item:last-child { border-bottom: 0; }\n.item .f { color: var(--muted); flex: none; }\n.item .d { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.item .bs { color: var(--red); flex: none; } .item .u { color: var(--muted); flex: none; }\n\n/* Zona inferior */\n.bottom { padding: 6px 12px calc(10px + env(safe-area-inset-bottom)); }\n.chips { display: flex; gap: 8px; overflow-x: auto; padding: 4px 2px 10px; scrollbar-width: none; }\n.chips::-webkit-scrollbar { display: none; }\n.chip { flex: none; padding: 8px 14px; border-radius: 999px; font-size: .92rem; background: var(--glass); border: 1px solid var(--glass-border);\n        -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); }\n.chip:active { background: var(--mint-soft); }\n.composer { display: flex; align-items: center; gap: 8px; padding: 6px 6px 6px 16px; border-radius: 999px; }\n#texto { flex: 1; min-width: 0; font-size: 16px; color: var(--text); background: none; border: 0; outline: none; padding: 8px 0; }\n#texto::placeholder { color: var(--muted); }\n.send { width: 40px; height: 40px; border-radius: 50%; background: var(--mint); color: #052e22; font-size: 1.05rem; }\n.send:disabled { opacity: .45; }\n:focus-visible { outline: 2px solid var(--mint); outline-offset: 2px; }\n\n@keyframes pop { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }\n@keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }\n@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }\n'.encode('utf-8'),
    'static/app.js': '/* GastosBot — frontend. Todo el texto dinámico se inserta con DOM (textContent), nunca con innerHTML. */\n(() => {\n  "use strict";\n  const $ = (id) => document.getElementById(id);\n  const NS = "http://www.w3.org/2000/svg";\n  const fmt = (n) => Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });\n  const norm = (t) => String(t).normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();\n  const state = { busy: false, onboarded: false };\n\n  function h(tag, props, ...kids) {\n    const el = document.createElement(tag);\n    for (const [k, v] of Object.entries(props || {})) {\n      if (v == null || v === false) continue;\n      if (k === "class") el.className = v;\n      else if (k === "text") el.textContent = v;\n      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);\n      else el.setAttribute(k, v);\n    }\n    for (const kid of kids.flat()) if (kid != null) el.append(kid);\n    return el;\n  }\n  function svg(tag, attrs) {\n    const el = document.createElementNS(NS, tag);\n    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);\n    return el;\n  }\n  function rich(texto) { // **negrita** -> <strong> (seguro)\n    const frag = document.createDocumentFragment();\n    String(texto).split(/\\*\\*(.+?)\\*\\*/g).forEach((p, i) => {\n      if (p) frag.append(i % 2 ? h("strong", { text: p }) : document.createTextNode(p));\n    });\n    return frag;\n  }\n\n  const ICONOS = { gasolina: "⛽", comida: "🍔", supermercado: "🛒", servicios: "💡", salud: "💊", cafe: "☕",\n    taxi: "🚕", uber: "🚕", transporte: "🚌", entretenimiento: "🎉", restaurante: "🍽️", sueldo: "💰", ingreso: "💰" };\n  const icono = (cat) => ICONOS[norm(cat)] || "🧾";\n\n  /* ---------- red ---------- */\n  class ApiError extends Error {\n    constructor(kind, message, status) { super(message); this.kind = kind; this.status = status || 0; }\n  }\n  async function api(path, { method = "GET", body, timeout = 15000 } = {}) {\n    if (typeof navigator !== "undefined" && navigator.onLine === false)\n      throw new ApiError("offline", "Sin conexión. Revisa tu internet e inténtalo de nuevo.");\n    const ctl = new AbortController();\n    const timer = setTimeout(() => ctl.abort(), timeout);\n    try {\n      return await fetch(path, { method, credentials: "same-origin", signal: ctl.signal,\n        headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });\n    } catch (e) {\n      if (e && e.name === "AbortError") throw new ApiError("timeout", "El servidor tardó demasiado en responder.");\n      throw new ApiError("network", "No pude conectar con el servidor.");\n    } finally { clearTimeout(timer); }\n  }\n  async function apiJson(path, opts) {\n    const res = await api(path, opts);\n    let data = null;\n    try { data = await res.json(); } catch (_) { /* no era JSON */ }\n    if (!res.ok) {\n      if (res.status === 401 && path !== "/api/auth") { bloquear("Tu sesión expiró. Ingresa el PIN de nuevo."); }\n      const msg = (data && data.error) || (res.status >= 500 ? "El servidor tuvo un problema. Intenta de nuevo." : `Error inesperado (${res.status}).`);\n      throw new ApiError(res.status === 503 ? "unavailable" : "http", msg, res.status);\n    }\n    if (data === null || typeof data !== "object") throw new ApiError("badjson", "Respuesta inválida del servidor.");\n    return data;\n  }\n\n  /* ---------- chat ---------- */\n  const chat = () => $("chat");\n  function bajar() {\n    const c = chat();\n    requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });\n  }\n  function agregar(el, { top = false } = {}) {\n    chat().append(el);\n    if (top && el.scrollIntoView) requestAnimationFrame(() => el.scrollIntoView({ block: "start", behavior: "smooth" }));\n    else bajar();\n    return el;\n  }\n  const burbujaUsuario = (t) => agregar(h("div", { class: "msg user", text: t }));\n  const burbujaBot = (t) => agregar(h("div", { class: "msg bot glass" }, rich(t)));\n  function burbujaError(e, reintentar) {\n    let msg = e.message || "Ocurrió un error.";\n    if (reintentar && (e.kind === "timeout" || e.kind === "network"))\n      msg += " Si era un gasto, revisa con «reporte» antes de reintentar para no duplicarlo.";\n    const b = h("div", { class: "msg bot glass err", role: "alert" }, rich("⚠️ " + msg));\n    if (reintentar) b.append(h("button", { class: "retry", type: "button", text: "Reintentar",\n      onclick: () => { b.remove(); reintentar(); } }));\n    return agregar(b);\n  }\n  function escribiendo() {\n    return agregar(h("div", { class: "msg bot glass typing", "aria-label": "Escribiendo" }, h("span"), h("span"), h("span")));\n  }\n\n  function burbujaMovimiento(data) {\n    const m = data.movimiento, ingreso = m.tipo === "ingreso";\n    const notas = String(data.respuesta || "").split("\\n").filter((l) => l.startsWith("⚠️") || l.startsWith("🚨"));\n    const [d, mo, y] = [m.fecha.slice(8, 10), m.fecha.slice(5, 7), m.fecha.slice(0, 4)];\n    const b = h("div", { class: "msg bot glass mov mov-wrap" },\n      h("span", { class: "emoji", "aria-hidden": "true", text: ingreso ? "💰" : icono(m.categoria) }),\n      h("div", { class: "cuerpo" },\n        h("div", { class: "cat", text: m.categoria }),\n        m.descripcion ? h("div", { class: "desc", text: m.descripcion }) : null,\n        h("div", { class: "meta", text: `Bs. ${fmt(m.monto_bs)} · tasa ${fmt(m.tasa)} · ${d}/${mo}/${y}` })),\n      h("div", { class: "der" },\n        h("div", { class: "usd" + (ingreso ? " pos" : ""), text: `${ingreso ? "+" : "-"}$${fmt(m.monto_usd)}` }),\n        h("div", { class: "meta", text: m.hora })),\n      notas.map((n) => h("div", { class: "nota", text: n })));\n    return agregar(b);\n  }\n\n  /* ---------- resumen (saldo, ahorro, presupuestos) ---------- */\n  function renderResumen(r) {\n    const box = $("resumen");\n    if (!r.onboarding_completo) { box.hidden = true; return; }\n    state.onboarded = true;\n    const R = 31, C = 2 * Math.PI * R;\n    const frac = r.ingresos_mes_usd > 0 ? Math.max(0, Math.min(1, r.ahorro_mes_usd / r.ingresos_mes_usd)) : 0;\n    const prog = svg("circle", { class: "prog", cx: 37, cy: 37, r: R, "stroke-dasharray": C.toFixed(2), "stroke-dashoffset": (C * (1 - frac)).toFixed(2) });\n    const ring = svg("svg", { class: "ring", viewBox: "0 0 74 74", role: "img", "aria-label": `Ahorro del mes: ${Math.round(frac * 100)}% de los ingresos` });\n    ring.append(svg("circle", { class: "track", cx: 37, cy: 37, r: R }), prog);\n    const nodos = [h("div", { class: "glass saldo" }, ring,\n      h("div", null, h("div", { class: "label", text: "Saldo Disponible" }), h("div", { class: "monto", text: "$" + fmt(r.saldo_usd) }),\n        h("div", { class: "sub", text: "Ahorro del mes: $" + fmt(r.ahorro_mes_usd) })))];\n    for (const p of r.presupuestos || []) {\n      const pct = p.limite_usd > 0 ? p.gastado_usd / p.limite_usd : 0;\n      const bar = h("div", { class: "bar" + (pct >= 1 ? " over" : pct >= 0.8 ? " warn" : "") }, h("i"));\n      bar.firstChild.style.width = Math.min(100, pct * 100).toFixed(1) + "%";\n      nodos.push(h("div", { class: "glass presu" },\n        h("div", { class: "fila" }, h("span", { class: "nombre", text: `${icono(p.categoria)} Presupuesto ${p.categoria}` }),\n          h("span", { text: `-$${fmt(p.gastado_usd)} / -$${fmt(p.limite_usd)}` })), bar));\n    }\n    box.replaceChildren(...nodos);\n    box.hidden = false;\n  }\n  async function refrescarResumen() {\n    try { renderResumen(await apiJson("/api/resumen")); } catch (_) { /* el resumen es secundario */ }\n  }\n\n  /* ---------- reporte (modelo 3: píldoras desplegables) ---------- */\n  const COLORES = ["#34D399", "#38BDF8", "#A78BFA", "#FBBF24", "#F472B6", "#94A3B8"];\n  function donut(cats, total) {\n    const orden = Object.entries(cats).map(([n, v]) => [n, v.total_bs]);\n    const top = orden.slice(0, 5), resto = orden.slice(5).reduce((a, [, v]) => a + v, 0);\n    if (resto > 0) top.push(["Otros", resto]);\n    const s = svg("svg", { class: "donut", viewBox: "0 0 36 36", role: "img", "aria-label": "Distribución del gasto por categoría" });\n    const ley = h("div", { class: "leyenda" });\n    let acum = 0;\n    top.forEach(([n, v], i) => {\n      const p = (v / total) * 100, color = COLORES[i % COLORES.length];\n      s.append(svg("circle", { cx: 18, cy: 18, r: 15.9155, stroke: color, "stroke-dasharray": `${Math.max(p - 0.6, 0.1).toFixed(2)} ${(100 - Math.max(p - 0.6, 0.1)).toFixed(2)}`, "stroke-dashoffset": (-acum).toFixed(2) }));\n      acum += p;\n      const sw = h("i"); sw.style.background = color;\n      ley.append(h("div", null, sw, `${n} ${p.toFixed(1)}%`));\n    });\n    return h("div", { class: "donut-row" }, s, ley);\n  }\n  function burbujaReporte(rep) {\n    const anual = /^Resumen Anual/.test(rep.titulo);\n    const card = h("div", { class: "glass rep-card" },\n      h("div", { class: "rt", text: rep.titulo }),\n      h("div", { class: "tot", text: "Total Bs. " + fmt(rep.total_bs) }),\n      h("div", { class: "tot", text: "Total USD " + fmt(rep.total_usd) }),\n      rep.tasa_promedio ? h("div", { class: "prom", text: `Tasa promedio: ${fmt(rep.tasa_promedio)} Bs/USD` }) : null,\n      rep.total_bs > 0 ? donut(rep.categorias, rep.total_bs) : null);\n    const wrap = h("div", { class: "reporte" }, card);\n    const pills = [];\n    Object.entries(rep.categorias).forEach(([cat, v], i) => {\n      const id = "pan-" + Date.now() + "-" + i;\n      const head = h("button", { class: "pill-head", type: "button", "aria-expanded": "false", "aria-controls": id },\n        h("span", { "aria-hidden": "true", text: icono(cat) }), h("span", { class: "t", text: `${cat} | -${fmt(v.total_bs)} Bs.` }),\n        h("span", { class: "chev", "aria-hidden": "true", text: "⌄" }));\n      const items = h("div", { class: "items" }, v.items.map((it) =>\n        h("div", { class: "item" }, h("span", { class: "f", text: anual ? it.fecha : it.fecha.slice(0, 5) }),\n          h("span", { class: "d", text: it.descripcion || cat }), h("span", { class: "bs", text: `-${fmt(it.monto_bs)} Bs.` }),\n          h("span", { class: "u", text: `($${fmt(it.monto_usd)})` }))));\n      const pill = h("div", { class: "pill" }, head, h("div", { class: "panel", id }, h("div", { class: "panel-inner" }, items)));\n      head.addEventListener("click", () => {\n        const abrir = !pill.classList.contains("open");\n        pills.forEach((p) => { p.classList.remove("open"); p.firstChild.setAttribute("aria-expanded", "false"); });\n        if (abrir) { pill.classList.add("open"); head.setAttribute("aria-expanded", "true"); }\n      });\n      pills.push(pill); wrap.append(pill);\n    });\n    return agregar(wrap, { top: true });\n  }\n\n  /* ---------- descarga CSV ---------- */\n  async function descargarCSV() {\n    try {\n      const res = await api("/api/exportar");\n      if (!res.ok) {\n        if (res.status === 401) bloquear("Tu sesión expiró. Ingresa el PIN de nuevo.");\n        let d = null; try { d = await res.json(); } catch (_) {}\n        throw new ApiError("http", (d && d.error) || `No se pudo generar el archivo (${res.status}).`, res.status);\n      }\n      const blob = await res.blob();\n      const m = /filename="?([^";]+)"?/.exec(res.headers.get("Content-Disposition") || "");\n      const nombre = m ? m[1] : "gastos.csv";\n      const url = URL.createObjectURL(blob);\n      const a = h("a", { href: url, download: nombre });\n      document.body.append(a); a.click(); a.remove();\n      setTimeout(() => URL.revokeObjectURL(url), 5000);\n      burbujaBot(`✅ Archivo listo: **${nombre}** (ábrelo con Excel o Google Sheets).`);\n    } catch (e) { burbujaError(e instanceof ApiError ? e : new ApiError("network", "No pude descargar el archivo."), descargarCSV); }\n  }\n\n  /* ---------- envío ---------- */\n  function ocupado(v) { state.busy = v; $("enviar").disabled = v; $("btn-reporte").disabled = v; }\n  async function enviar(texto, visible, reintento = false) {\n    texto = String(texto).trim();\n    if (!texto || state.busy) return;\n    if (!reintento) burbujaUsuario(visible || texto);\n    ocupado(true);\n    const t = escribiendo();\n    try {\n      const data = await apiJson("/api/chat", { method: "POST", body: { mensaje: texto } });\n      t.remove();\n      if (data.tipo === "reporte" && data.reporte) burbujaReporte(data.reporte);\n      else if (data.tipo === "descarga") { burbujaBot(data.respuesta || "Descargando…"); await descargarCSV(); }\n      else if (data.movimiento) burbujaMovimiento(data);\n      else burbujaBot(data.respuesta || "…");\n      if (data.tipo !== "reporte") refrescarResumen();\n    } catch (e) {\n      t.remove();\n      if (!$("app").hidden) burbujaError(e, () => enviar(texto, visible, true));\n    } finally { ocupado(false); }\n  }\n\n  /* ---------- PIN ---------- */\n  function bloquear(msg) {\n    $("app").hidden = true; $("lock").hidden = false; $("pin").value = "";\n    const e = $("pin-error"); e.textContent = msg || ""; e.hidden = !msg;\n  }\n  async function entrar() {\n    const pin = $("pin").value.trim(), err = $("pin-error");\n    err.hidden = true;\n    if (!/^\\d{4,6}$/.test(pin)) { err.textContent = "Ingresa tu PIN de 4 a 6 dígitos."; err.hidden = false; return; }\n    $("pin-btn").disabled = true;\n    try {\n      const d = await apiJson("/api/auth", { method: "POST", body: { pin } });\n      $("pin").value = ""; $("lock").hidden = true; $("app").hidden = false;\n      chat().querySelectorAll(".msg, .reporte").forEach((n) => n.remove());\n      burbujaBot(d.mensaje || "¡Hola!");\n      if (d.onboarding_completo) refrescarResumen(); else $("resumen").hidden = true;\n      ajustarTeclado();\n    } catch (e) { err.textContent = e.message; err.hidden = false; $("pin").value = ""; }\n    finally { $("pin-btn").disabled = false; }\n  }\n\n  /* ---------- teclado virtual iOS ---------- */\n  function ajustarTeclado() {\n    const vv = window.visualViewport, root = document.documentElement.style;\n    root.setProperty("--app-h", (vv ? vv.height : window.innerHeight) + "px");\n    root.setProperty("--app-top", (vv ? vv.offsetTop : 0) + "px");\n    bajar();\n  }\n\n  /* ---------- init ---------- */\n  function init() {\n    $("pin-btn").addEventListener("click", entrar);\n    $("pin").addEventListener("keydown", (e) => { if (e.key === "Enter") entrar(); });\n    $("form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("texto").value; $("texto").value = ""; enviar(t); });\n    $("btn-reporte").addEventListener("click", () => enviar("reporte", "Genera mi reporte de gastos"));\n    $("chips").addEventListener("click", (e) => {\n      const b = e.target.closest(".chip"); if (!b) return;\n      const i = $("texto"); i.value = b.dataset.prefijo; i.focus();\n    });\n    if (window.visualViewport) { window.visualViewport.addEventListener("resize", ajustarTeclado); window.visualViewport.addEventListener("scroll", ajustarTeclado); }\n    window.addEventListener("resize", ajustarTeclado);\n    window.addEventListener("orientationchange", ajustarTeclado);\n    ajustarTeclado();\n    $("pin").focus();\n  }\n  function registrarSW() {\n    if (!("serviceWorker" in navigator)) return;\n    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* la app funciona sin SW */ }));\n  }\n  registrarSW();\n  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();\n})();\n'.encode('utf-8'),
    'static/sw.js': '/* Service worker de GastosBot. Solo cachea el "cascarón" de la app; NUNCA datos (/api) ni sesiones. */\nconst VERSION = "__VERSION__";\nconst CACHE = "gastosbot-" + VERSION;\nconst SHELL = ["/", "/static/app.css", "/static/app.js", "/static/manifest.json",\n  "/static/icons/icon-192.png", "/static/icons/apple-touch-icon.png"];\n\nself.addEventListener("install", (e) => {\n  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));\n});\n\nself.addEventListener("activate", (e) => {\n  e.waitUntil(caches.keys()\n    .then((ks) => Promise.all(ks.filter((k) => k.startsWith("gastosbot-") && k !== CACHE).map((k) => caches.delete(k))))\n    .then(() => self.clients.claim()));\n});\n\nself.addEventListener("fetch", (e) => {\n  const req = e.request, url = new URL(req.url);\n  if (req.method !== "GET" || url.origin !== self.location.origin) return;\n  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz" || url.pathname === "/sw.js") return; // siempre red\n  if (req.mode === "navigate") { // red primero; sin conexión, el cascarón guardado\n    e.respondWith(fetch(req).then((r) => { if (r.ok) { const cp = r.clone(); caches.open(CACHE).then((c) => c.put("/", cp)); } return r; })\n      .catch(() => caches.match("/").then((r) => r || new Response("Sin conexión", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }))));\n    return;\n  }\n  e.respondWith(caches.match(req).then((hit) => {\n    const red = fetch(req).then((r) => { if (r.ok) { const cp = r.clone(); caches.open(CACHE).then((c) => c.put(req, cp)); } return r; })\n      .catch(() => null); // el refresco en segundo plano nunca debe lanzar errores sin capturar\n    return hit || red.then((r) => r || Response.error());\n  }));\n});\n'.encode('utf-8'),
    'static/manifest.json': '{\n  "id": "/",\n  "name": "GastosBot",\n  "short_name": "GastosBot",\n  "description": "Registra y consulta tus gastos conversando.",\n  "lang": "es",\n  "start_url": "/",\n  "scope": "/",\n  "display": "standalone",\n  "orientation": "portrait",\n  "background_color": "#0F172A",\n  "theme_color": "#0F172A",\n  "categories": ["finance", "productivity"],\n  "icons": [\n    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },\n    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },\n    { "src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }\n  ]\n}\n'.encode('utf-8'),
    'static/icons/apple-touch-icon.png': base64.b64decode("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"),
    'static/icons/icon-192.png': base64.b64decode("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"),
    'static/icons/icon-512.png': base64.b64decode("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"),
    'static/icons/icon-maskable-512.png': base64.b64decode("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"),
}


def version():
    """Hash del contenido: cambia el caché del service worker al desplegar cambios."""
    h = hashlib.sha1()
    for clave in sorted(ASSETS):
        if clave != "static/sw.js":
            h.update(clave.encode() + ASSETS[clave])
    return h.hexdigest()[:10]


# ======== módulo: app ========
app_log = logging.getLogger("gastosbot")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; worker-src 'self'; manifest-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")


def _dec(texto):
    try:
        v = Decimal(str(texto).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    return v if v.is_finite() else None


def _txt(msg, code=200):
    return jsonify({"tipo": "texto", "respuesta": msg}), code


def _csv_seguro(v):
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".json": "application/json", ".png": "image/png"}


def _servir(clave):
    """Sirve un recurso incrustado (nunca toca el disco: no hay path traversal posible)."""
    datos = ASSETS.get(clave)
    if datos is None:
        abort(404)
    r = Response(datos, content_type=MIME[os.path.splitext(clave)[1]])
    r.set_etag(hashlib.sha1(datos).hexdigest())
    r.headers["Cache-Control"] = "no-cache"
    return r.make_conditional(request)


def create_app(env=None, db=None, now_fn=None):
    cfg = load_config(env)
    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=cfg["secret_key"], SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg["prod"], PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=16 * 1024)
    app.json.sort_keys = False  # conserva el orden de categorías del reporte
    app.json.ensure_ascii = False
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db = db or Database(cfg["database_url"])
    ahora = now_fn or make_clock(cfg["tz"])
    app.extensions["db"] = db
    sw_text = ASSETS["static/sw.js"].decode("utf-8").replace("__VERSION__", version())
    try:
        db.init(retries=3)
    except SQLAlchemyError:
        app_log.error("BD no disponible al arrancar; se reintentará por petición")

    @app.before_request
    def guardas():
        if not request.path.startswith("/api/"):
            return None
        if request.method in ("POST", "PUT") and not request.is_json:
            return jsonify(error="Content-Type debe ser application/json"), 415
        if not db.ensure_ready():
            return jsonify(error="Base de datos no disponible, intenta en unos segundos"), 503
        if request.path != "/api/auth" and not session.get("autenticado"):
            return jsonify(error="No autorizado"), 401
        return None

    @app.after_request
    def cabeceras(r):
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Referrer-Policy"] = "no-referrer"
        r.headers["Content-Security-Policy"] = CSP
        if request.path.startswith("/api/"):
            r.headers["Cache-Control"] = "no-store"
        return r

    @app.errorhandler(OperationalError)
    def e_conexion(e):
        app_log.error("BD no disponible: %s", e.__class__.__name__)
        return jsonify(error="Base de datos no disponible, intenta en unos segundos"), 503

    @app.errorhandler(SQLAlchemyError)
    def e_sql(e):
        app_log.exception("Error de BD")
        return jsonify(error="Error al guardar. No se modificó nada."), 500

    @app.errorhandler(HTTPException)
    def e_http(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description), e.code
        return e

    @app.errorhandler(Exception)
    def e_generico(e):
        app_log.exception("Error no controlado")
        return jsonify(error="Error interno"), 500

    # ---------- páginas ----------
    @app.get("/")
    def index():
        return _servir("index.html")

    @app.get("/sw.js")
    def service_worker():
        r = Response(sw_text, mimetype="text/javascript")
        r.headers["Cache-Control"] = "no-cache"
        r.headers["Service-Worker-Allowed"] = "/"
        return r

    @app.get("/favicon.ico")
    def favicon():
        return _servir("static/icons/icon-192.png")

    @app.get("/static/<path:ruta>")
    def estaticos(ruta):
        return _servir("static/" + ruta)

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, db=db.ready)

    # ---------- auth ----------
    @app.post("/api/auth")
    def auth():
        data = request.get_json(silent=True) or {}
        ip, ts = request.remote_addr or "?", ahora()
        with db.session() as s:
            espera = segundos_bloqueo(s, ip, ts)
            if espera:
                r = jsonify(ok=False, error=f"Demasiados intentos. Espera {math.ceil(espera / 60)} min.")
                r.status_code = 429
                r.headers["Retry-After"] = str(espera)
                return r
            if not pin_valido(data.get("pin"), cfg["pin"]):
                registrar_fallo(s, ip, ts)
                return jsonify(ok=False, error="PIN incorrecto"), 401
            limpiar(s, ip)
            session.clear()
            session["autenticado"] = True
            session.permanent = True
            c = s.query(Configuracion).first()
            tasa = tasa_para_fecha(s, ts.date())
            completo = bool(c and c.nombre_usuario and tasa)
            if completo:
                msg = f"{generar_saludo(c.nombre_usuario, ts.hour)} (Tasa vigente: Bs. {tasa:,.2f})"
            elif not c or not c.nombre_usuario:
                msg = "¡Hola! Soy GastosBot. Antes de empezar, ¿cómo te llamas?"
            else:
                msg = f"¡Hola {c.nombre_usuario}! Por favor dime cuál es la tasa de cambio de hoy (ej: tasa 915)."
            return jsonify(ok=True, onboarding_completo=completo, mensaje=msg)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    # ---------- chat ----------
    @app.post("/api/chat")
    def chat():
        texto = str((request.get_json(silent=True) or {}).get("mensaje", "")).strip()
        if not texto:
            return _txt("Escribe un gasto o pide un reporte.")
        if len(texto) > 300:
            return _txt("Mensaje demasiado largo (máx. 300 caracteres).")
        with db.session() as s:
            payload, code = procesar(s, texto, ahora())
            return jsonify(payload), code

    # ---------- resumen y presupuestos ----------
    @app.get("/api/resumen")
    def resumen():
        with db.session() as s:
            return jsonify(services_resumen(s, ahora().date()))

    @app.put("/api/presupuestos")
    def presupuestos():
        d = request.get_json(silent=True) or {}
        cat = str(d.get("categoria", "")).strip().split(" ")[0][:100]
        lim = _dec(d.get("limite_usd", ""))
        if not cat or lim is None or lim < 0 or lim >= MAX_MONTO:
            return jsonify(error="Indica categoría y un límite en USD (0 para eliminar)"), 400
        with db.session() as s:
            fijar_presupuesto(s, canon_categoria(cat), lim)
        return jsonify(ok=True)

    # ---------- exportar ----------
    @app.get("/api/exportar")
    def exportar():
        hoy = ahora().date()
        with db.session() as s:
            filas = list(reversed(
                [m for t in ("gasto", "ingreso") for m in movimientos(s, t, *rango(hoy.month, hoy.year))]))
            filas.sort(key=lambda m: (m.fecha, m.id))
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["Fecha", "Tipo", "Categoria", "Descripcion", "Monto_Bs", "Tasa_Aplicada", "Monto_USD"])
            for m in filas:
                w.writerow([m.fecha.isoformat(), m.tipo, _csv_seguro(m.categoria), _csv_seguro(m.descripcion or ""),
                            f"{m.monto_bs:.2f}", f"{m.tasa_aplicada:.4f}", f"{m.monto_usd:.2f}"])
        return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=gastos_{hoy:%Y-%m}.csv"})

    return app


def main():
    """Arranque local directo: python app.py (en producción se usa gunicorn app:app)."""
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")))


if __name__ == "__main__":
    main()
elif os.environ.get("GASTOSBOT_NO_AUTOAPP") != "1":
    app = create_app()

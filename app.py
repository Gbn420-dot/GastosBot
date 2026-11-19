"""GastosBot — app.py generado.
ARCHIVO GENERADO por tools/build_single.py (fuente 7a8503edc4ba). NO EDITAR A MANO."""
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
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, MetaData, Numeric, String, Table, UniqueConstraint, create_engine, event, inspect, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
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
        faltan = [n for n, v in (("SECRET_KEY", secret), ("DATABASE_URL", url)) if not v]   # APP_PIN es opcional
        if faltan:
            raise ConfigError("Faltan variables de entorno en producción: " + ", ".join(faltan))
        if len(secret) < 16:
            raise ConfigError("SECRET_KEY debe tener al menos 16 caracteres")
    else:  # desarrollo local
        secret = secret or "solo-desarrollo-local"
        url = url or "sqlite:///local_test.db"

    pin_defecto = not pin
    pin = pin or "1234"   # PIN inicial si no hay APP_PIN; cada usuario lo cambia desde ⚙️ Configuración
    if not re.fullmatch(r"\d{4,6}", str(pin)):
        raise ConfigError("APP_PIN debe ser numérico de 4 a 6 dígitos")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    tz = env.get("APP_TZ", "America/Caracas")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"APP_TZ inválida: {tz}")
    return {"prod": prod, "pin": str(pin), "secret_key": secret, "database_url": url, "tz": tz, "pin_defecto": pin_defecto}


# ======== módulo: clock ========
"""Reloj en la zona horaria del cliente (Render corre en UTC)."""




def make_clock(tz):
    zona = ZoneInfo(tz)
    return lambda: datetime.now(zona).replace(tzinfo=None)


# ======== módulo: models ========
Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Esquema(Base):
    """Versión del esquema de datos (una sola fila, id=1)."""
    __tablename__ = "esquema"
    id = Column(Integer, primary_key=True)
    version = Column(Integer, nullable=False)


class Usuario(Base):
    """Máximo 2. nombre NULL = dueño pendiente de los datos anteriores (lo reclama el primer registro).
    pin_hash NULL = usa el PIN inicial (APP_PIN o 1234)."""
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    nombre = Column(String(40), nullable=True)
    nombre_norm = Column(String(40), unique=True, nullable=True)
    pin_hash = Column(String(200), nullable=True)
    creado_en = Column(DateTime, default=_utcnow)


class Categoria(Base):
    """Árbol por usuario. Raíces fijas: PERSONAL, NEGOCIOS, SIN CLASIFICAR. Un local es hijo directo de NEGOCIOS."""
    __tablename__ = "categorias"
    __table_args__ = (UniqueConstraint("user_id", "parent_id", "nombre_norm", name="uq_cat_hermanos"),
                      Index("ix_cat_user", "user_id"))
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("categorias.id"), nullable=True)
    nombre = Column(String(100), nullable=False)
    nombre_norm = Column(String(100), nullable=False)
    icono = Column(String(16), nullable=False, default="📁")
    es_local = Column(Boolean, nullable=False, default=False)
    fija = Column(Boolean, nullable=False, default=False)
    orden = Column(Integer, nullable=False, default=0)
    creado_en = Column(DateTime, default=_utcnow)


class Ajustes(Base):
    __tablename__ = "ajustes"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False, unique=True)
    saldo_inicial_usd = Column(Numeric(16, 2), nullable=False, default=0)


class TasaCambio(Base):
    __tablename__ = "tasas"
    __table_args__ = (CheckConstraint("valor > 0", name="ck_tasa_positiva"),
                      UniqueConstraint("user_id", "fecha", name="uq_tasa_user_fecha"))
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha = Column(Date, nullable=False)
    valor = Column(Numeric(14, 4), nullable=False)


class Movimiento(Base):
    """Gasto, ingreso o depósito. Tasa y USD quedan congelados al registrar. eliminado_en != NULL = en papelera."""
    __tablename__ = "registros"
    __table_args__ = (
        CheckConstraint("tipo IN ('gasto','ingreso','deposito')", name="ck_mov_tipo"),
        CheckConstraint("monto_bs > 0", name="ck_mov_monto"),
        Index("ix_reg_user_fecha", "user_id", "fecha", "tipo"),
        Index("ix_reg_cat", "categoria_id"),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    tipo = Column(String(10), nullable=False, default="gasto")
    categoria_id = Column(Integer, ForeignKey("categorias.id"), nullable=False)
    descripcion = Column(String(200), nullable=True)
    monto_bs = Column(Numeric(16, 2), nullable=False)
    tasa_aplicada = Column(Numeric(14, 4), nullable=False)
    monto_usd = Column(Numeric(16, 2), nullable=False)
    fecha = Column(Date, nullable=False)
    creado_en = Column(DateTime, default=_utcnow)
    eliminado_en = Column(DateTime, nullable=True)
    cat = relationship("Categoria", lazy="joined", innerjoin=True)

    @property
    def categoria(self):
        return self.cat.nombre if self.cat is not None else None


class Presupuesto(Base):
    __tablename__ = "limites"
    __table_args__ = (CheckConstraint("limite_usd > 0", name="ck_pres_limite"),
                      UniqueConstraint("user_id", "categoria_id", name="uq_lim_user_cat"))
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    categoria_id = Column(Integer, ForeignKey("categorias.id"), nullable=False)
    limite_usd = Column(Numeric(16, 2), nullable=False)
    cat = relationship("Categoria", lazy="joined", innerjoin=True)

    @property
    def categoria(self):
        return self.cat.nombre


class Factura(Base):
    """Datos de una factura (sin imagen). monto en su moneda; solo_bs = nunca se convierte a dólares."""
    __tablename__ = "facturas"
    __table_args__ = (
        CheckConstraint("monto > 0", name="ck_fac_monto"),
        CheckConstraint("moneda IN ('bs','usd')", name="ck_fac_moneda"),
        CheckConstraint("NOT solo_bs OR moneda = 'bs'", name="ck_fac_solo_bs"),
        CheckConstraint("dias_credito IS NULL OR (dias_credito >= 0 AND dias_credito <= 365)", name="ck_fac_credito"),
        Index("ix_fac_user", "user_id", "eliminado_en"),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    numero = Column(String(40), nullable=False)
    numero_norm = Column(String(40), nullable=False)
    proveedor = Column(String(100), nullable=False)
    proveedor_norm = Column(String(100), nullable=False)
    fecha_emision = Column(Date, nullable=False)
    monto = Column(Numeric(16, 2), nullable=False)
    moneda = Column(String(3), nullable=False, default="bs")
    solo_bs = Column(Boolean, nullable=False, default=False)
    dias_credito = Column(Integer, nullable=True)
    categoria_id = Column(Integer, ForeignKey("categorias.id"), nullable=False)
    nota = Column(String(200), nullable=True)
    creado_en = Column(DateTime, default=_utcnow)
    eliminado_en = Column(DateTime, nullable=True)


class Abono(Base):
    """Pago parcial o total de una factura (monto en la moneda de la factura). Crea un gasto vinculado."""
    __tablename__ = "abonos"
    __table_args__ = (CheckConstraint("monto > 0", name="ck_abono_monto"), Index("ix_abono_fac", "factura_id"))
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    factura_id = Column(Integer, ForeignKey("facturas.id"), nullable=False)
    fecha = Column(Date, nullable=False)
    monto = Column(Numeric(16, 2), nullable=False)
    movimiento_id = Column(Integer, ForeignKey("registros.id"), nullable=True)
    creado_en = Column(DateTime, default=_utcnow)
    eliminado_en = Column(DateTime, nullable=True)


class Preferencia(Base):
    """Preferencias por usuario (clave/valor): solo_bs_defecto, dias_aviso, ..."""
    __tablename__ = "preferencias"
    __table_args__ = (UniqueConstraint("user_id", "clave", name="uq_pref_user_clave"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    clave = Column(String(40), nullable=False)
    valor = Column(String(100), nullable=False)


class IntentoAuth(Base):
    """Contadores anti fuerza bruta (compartidos entre workers vía BD)."""
    __tablename__ = "intentos_auth"
    clave = Column(String(64), primary_key=True)  # IP o '*' (global)
    fallos = Column(Integer, nullable=False, default=0)
    bloqueado_hasta = Column(DateTime, nullable=True)
    actualizado = Column(DateTime, nullable=True)


# ======== módulo: arbol ========
"""Árbol de categorías por usuario (PERSONAL / NEGOCIOS / locales) y su aritmética. Todo filtrado por user_id."""





MAX_PROFUNDIDAD = 8
MAX_NOMBRE = 60
RAICES = [("PERSONAL", "👤"), ("NEGOCIOS", "🏪"), ("SIN CLASIFICAR", "📥")]
PREDETERMINADAS = [("Gasolina", "⛽"), ("Comida", "🍔"), ("Supermercado", "🛒"), ("Servicios", "💡"), ("Salud", "💊")]
ICONO_DEFECTO = "📁"


class ArbolError(ValueError):
    """Error de regla de negocio del árbol (mensaje apto para mostrar al usuario)."""


def nombre_norm(t):
    t = unicodedata.normalize("NFD", str(t).casefold())
    return re.sub(r"\s+", " ", "".join(c for c in t if unicodedata.category(c) != "Mn")).strip()


def limpiar_nombre(t, maximo=MAX_NOMBRE):
    n = re.sub(r"\s+", " ", str(t or "")).strip()
    if not n or len(n) > maximo:
        raise ArbolError(f"El nombre debe tener de 1 a {maximo} caracteres.")
    if re.search(r"[<>\x00-\x1f]", n):
        raise ArbolError("El nombre no puede contener < > ni caracteres de control.")
    return n


def limpiar_icono(i):
    i = str(i or "").strip()
    if not i:
        return ICONO_DEFECTO
    if len(i) > 8 or re.search(r"[<>\x00-\x1f\s\w]", i):
        raise ArbolError("Ícono inválido (usa un emoji).")
    return i


def cargar(s, uid):
    return {c.id: c for c in s.query(Categoria).filter(Categoria.user_id == uid).all()}


def _get(cats, cid):
    c = cats.get(cid) if cid is not None else None
    if c is None:
        raise ArbolError("La categoría no existe.")
    return c


def arbol_ruta(cats, cid):
    """Nombres desde la raíz hasta la categoría. Tolera ciclos corruptos (corta al repetir)."""
    out, visto, c = [], set(), cats.get(cid)
    while c is not None and c.id not in visto:
        visto.add(c.id)
        out.append(c.nombre)
        c = cats.get(c.parent_id) if c.parent_id is not None else None
    return list(reversed(out))


def profundidad(cats, cid):
    return len(arbol_ruta(cats, cid))


def hijos_de(cats, cid):
    return sorted((c for c in cats.values() if c.parent_id == cid), key=lambda c: (c.orden, c.nombre_norm, c.id))


def ids_subarbol(cats, cid):
    """cid y todos sus descendientes (iterativo, seguro ante ciclos)."""
    res, pend = {cid}, [cid]
    while pend:
        p = pend.pop()
        for c in cats.values():
            if c.parent_id == p and c.id not in res:
                res.add(c.id)
                pend.append(c.id)
    return res


def raiz_de(cats, cid):
    c, visto = cats.get(cid), set()
    while c is not None and c.parent_id is not None and c.id not in visto:
        visto.add(c.id)
        c = cats.get(c.parent_id)
    return c


def local_de(cats, cid):
    """Local (hijo directo de NEGOCIOS) que contiene la categoría; None = personal, compartido o sin clasificar."""
    c, visto = cats.get(cid), set()
    while c is not None and c.id not in visto:
        visto.add(c.id)
        if c.es_local:
            return c
        c = cats.get(c.parent_id) if c.parent_id is not None else None
    return None


def raiz_por_nombre(cats, nombre):
    return next(c for c in cats.values() if c.parent_id is None and c.fija and c.nombre == nombre)


def sembrar(s, uid):
    """Crea raíces fijas y categorías iniciales. Idempotente."""
    if s.query(Categoria).filter(Categoria.user_id == uid).count():
        return
    raices = {}
    for i, (n, ic) in enumerate(RAICES):
        r = Categoria(user_id=uid, parent_id=None, nombre=n, nombre_norm=nombre_norm(n), icono=ic, fija=True, orden=i)
        s.add(r)
        raices[n] = r
    s.flush()
    for i, (n, ic) in enumerate(PREDETERMINADAS):
        s.add(Categoria(user_id=uid, parent_id=raices["PERSONAL"].id, nombre=n, nombre_norm=nombre_norm(n),
                        icono=ic, orden=i))
    s.flush()


def _hermano(cats, padre_id, nnorm, excepto=None):
    return next((c for c in cats.values() if c.parent_id == padre_id and c.nombre_norm == nnorm
                 and c.id != excepto), None)


def arbol_crear(s, uid, nombre, padre_id, icono=None, es_local=False):
    cats = cargar(s, uid)
    nombre = limpiar_nombre(nombre)
    padre = _get(cats, padre_id)  # no se pueden crear raíces nuevas
    if profundidad(cats, padre.id) >= MAX_PROFUNDIDAD:
        raise ArbolError(f"Profundidad máxima: {MAX_PROFUNDIDAD} niveles.")
    if _hermano(cats, padre.id, nombre_norm(nombre)):
        raise ArbolError(f"Ya existe «{nombre}» en ese nivel.")
    if es_local and not (padre.fija and padre.nombre == "NEGOCIOS"):
        raise ArbolError("Un local solo puede colgar directamente de NEGOCIOS.")
    c = Categoria(user_id=uid, parent_id=padre.id, nombre=nombre, nombre_norm=nombre_norm(nombre),
                  icono=limpiar_icono(icono), es_local=bool(es_local), orden=len(hijos_de(cats, padre.id)))
    s.add(c)
    s.flush()
    return c


def renombrar(s, uid, cid, nombre):
    cats = cargar(s, uid)
    c = _get(cats, cid)
    if c.fija:
        raise ArbolError("Las categorías base no se pueden renombrar.")
    nombre = limpiar_nombre(nombre)
    if _hermano(cats, c.parent_id, nombre_norm(nombre), excepto=c.id):
        raise ArbolError(f"Ya existe «{nombre}» en ese nivel.")
    c.nombre, c.nombre_norm = nombre, nombre_norm(nombre)
    s.flush()
    return c


def cambiar_icono(s, uid, cid, icono):
    c = _get(cargar(s, uid), cid)
    c.icono = limpiar_icono(icono)
    s.flush()
    return c


def mover(s, uid, cid, nuevo_padre_id):
    cats = cargar(s, uid)
    c, p = _get(cats, cid), _get(cats, nuevo_padre_id)
    if c.fija:
        raise ArbolError("Las categorías base no se pueden mover.")
    sub = ids_subarbol(cats, c.id)
    if p.id in sub:
        raise ArbolError("No puedes mover una categoría dentro de sí misma o de sus subcategorías.")
    alto = max(profundidad(cats, i) for i in sub) - profundidad(cats, c.id)
    if profundidad(cats, p.id) + 1 + alto > MAX_PROFUNDIDAD:
        raise ArbolError(f"Profundidad máxima: {MAX_PROFUNDIDAD} niveles.")
    if _hermano(cats, p.id, c.nombre_norm, excepto=c.id):
        raise ArbolError(f"Ya existe «{c.nombre}» en el destino.")
    if c.es_local and not (p.fija and p.nombre == "NEGOCIOS"):
        raise ArbolError("Un local solo puede estar directamente bajo NEGOCIOS.")
    if any(cats[i].es_local for i in sub if i != c.id):
        raise ArbolError("No puedes mover una categoría que contiene locales: los locales deben colgar de NEGOCIOS.")
    c.parent_id = p.id
    s.flush()
    return c


def marcar_local(s, uid, cid, valor):
    cats = cargar(s, uid)
    c = _get(cats, cid)
    padre = cats.get(c.parent_id)
    if valor and not (padre is not None and padre.fija and padre.nombre == "NEGOCIOS"):
        raise ArbolError("Solo las categorías directas de NEGOCIOS pueden ser locales.")
    c.es_local = bool(valor)
    s.flush()
    return c


def _reasignar_registros(s, uid, origen_ids, destino_id):
    for m in s.query(Movimiento).filter(Movimiento.user_id == uid, Movimiento.categoria_id.in_(origen_ids)).all():
        m.categoria_id = destino_id
    for f in s.query(Factura).filter(Factura.user_id == uid, Factura.categoria_id.in_(origen_ids)).all():
        f.categoria_id = destino_id


def _fusionar_limites(s, uid, origen_id, destino_id):
    o = s.query(Presupuesto).filter(Presupuesto.user_id == uid, Presupuesto.categoria_id == origen_id).first()
    if not o:
        return
    d = s.query(Presupuesto).filter(Presupuesto.user_id == uid, Presupuesto.categoria_id == destino_id).first()
    if d:
        s.delete(o)  # el destino conserva su límite
    else:
        o.categoria_id = destino_id


def fusionar(s, uid, origen_id, destino_id):
    """Pasa movimientos, límites e hijos de origen a destino (fusionando hijos homónimos) y borra origen."""
    cats = cargar(s, uid)
    o, d = _get(cats, origen_id), _get(cats, destino_id)
    if o.fija:
        raise ArbolError("Las categorías base no se pueden fusionar.")
    if o.id == d.id or d.id in ids_subarbol(cats, o.id):
        raise ArbolError("El destino no puede ser la misma categoría ni una subcategoría suya.")
    if o.es_local and not d.es_local:
        raise ArbolError("Un local solo puede fusionarse con otro local.")
    _fusionar_nodo(s, uid, cats, o, d)
    s.flush()
    return d


def _fusionar_nodo(s, uid, cats, o, d):
    _reasignar_registros(s, uid, [o.id], d.id)
    _fusionar_limites(s, uid, o.id, d.id)
    for h in hijos_de(cats, o.id):
        gemelo = _hermano(cats, d.id, h.nombre_norm)
        if gemelo:
            _fusionar_nodo(s, uid, cats, h, gemelo)
        else:
            h.parent_id = d.id
    s.flush()
    s.delete(o)
    s.flush()
    cats.pop(o.id, None)


def tiene_registros(s, uid, ids):
    ids = list(ids)
    return (s.query(Movimiento.id).filter(Movimiento.user_id == uid, Movimiento.categoria_id.in_(ids)).first() is not None
            or s.query(Factura.id).filter(Factura.user_id == uid, Factura.categoria_id.in_(ids)).first() is not None)


def arbol_eliminar(s, uid, cid, destino_id=None):
    """Sin destino: solo si la categoría y sus hijos están vacíos. Con destino: pasa todo allí y borra."""
    cats = cargar(s, uid)
    c = _get(cats, cid)
    if c.fija:
        raise ArbolError("Las categorías base no se pueden eliminar.")
    sub = ids_subarbol(cats, c.id)
    if destino_id is None:
        if tiene_registros(s, uid, sub) or len(sub) > 1:
            raise ArbolError("Tiene movimientos o subcategorías. Elige a dónde pasarlos o fusiónala.")
        for p in s.query(Presupuesto).filter(Presupuesto.user_id == uid, Presupuesto.categoria_id == c.id).all():
            s.delete(p)
        s.delete(c)
        s.flush()
        return
    fusionar(s, uid, c.id, destino_id)


def clave(texto):
    """Clave de comparación: sin tildes ni mayúsculas y con plural simple recortado (servicios = servicio)."""
    return " ".join(p[:-1] if len(p) > 3 and p.endswith("s") else p for p in nombre_norm(texto).split())


def resolver_ruta(cats, palabras):
    """Interpreta las primeras palabras como una ruta del árbol (NEGOCIOS Centro servicio gasolina).
    Cada palabra siguiente debe ser un descendiente de la anterior. Devuelve [(categoria, palabras_usadas)] con
    las coincidencias más específicas (varias = ambiguo); [] si la primera palabra no existe."""
    T = [clave(p) for p in palabras]
    idx = {}
    for c in cats.values():
        idx.setdefault(clave(c.nombre), []).append(c)

    def coincidencias(pos, dentro):
        for n in (3, 2, 1):
            if pos + n <= len(T):
                hall = [c for c in idx.get(" ".join(T[pos:pos + n]), []) if dentro is None or c.id in dentro]
                if hall:
                    return n, hall
        return 0, []

    finales = {}

    def extender(nodo, pos):
        sub = ids_subarbol(cats, nodo.id) - {nodo.id}
        n, hall = coincidencias(pos, sub) if sub and pos < len(T) else (0, [])
        if not hall:
            if nodo.id not in finales or finales[nodo.id][1] < pos:
                finales[nodo.id] = (nodo, pos)
            return
        for h in hall[:20]:
            extender(h, pos + n)

    n0, hall0 = coincidencias(0, None) if T else (0, [])
    for h in hall0[:20]:
        extender(h, n0)
    if not finales:
        return []
    mejor = max(p for _, p in finales.values())
    return sorted((v for v in finales.values() if v[1] == mejor), key=lambda v: (profundidad(cats, v[0].id), v[0].id))


def etiqueta_aqui(cats, c):
    if c.parent_id is None:
        return {"PERSONAL": "Personal (general)", "NEGOCIOS": "Compartido (todos los locales)"}.get(c.nombre, c.nombre)
    return f"General de «{c.nombre}»" if c.es_local else f"Solo «{c.nombre}»"


def buscar_por_nombre(cats, palabra):
    """Coincidencias exactas (sin tildes/mayúsculas), ordenadas por cercanía a la raíz y luego id."""
    n = nombre_norm(palabra)
    return sorted((c for c in cats.values() if c.nombre_norm == n and c.parent_id is not None),
                  key=lambda c: (profundidad(cats, c.id), c.id))


def resolver_o_crear(s, uid, palabra):
    """Categoría por nombre; si no existe se crea en SIN CLASIFICAR. Si hay varias, la más cercana a la raíz
    (el bloque de chat pedirá elegir con botones)."""
    cats = cargar(s, uid)
    hallados = buscar_por_nombre(cats, palabra)
    if hallados:
        return hallados[0]
    return arbol_crear(s, uid, palabra, raiz_por_nombre(cats, "SIN CLASIFICAR").id)


def arbol_json(s, uid):
    cats = cargar(s, uid)

    def nodo(c):
        return {"id": c.id, "nombre": c.nombre, "icono": c.icono, "es_local": bool(c.es_local), "fija": bool(c.fija),
                "hijos": [nodo(h) for h in hijos_de(cats, c.id)]}
    return [nodo(r) for r in sorted((c for c in cats.values() if c.parent_id is None), key=lambda c: (c.orden, c.id))]


def totales(s, uid, inicio, fin, tipo="gasto"):   # tipo: texto o tupla de tipos
    """{categoria_id: {'propio_bs','propio_usd','total_bs','total_usd'}} con subtotales jerárquicos (excluye papelera)."""
    cats = cargar(s, uid)
    propio = {}
    q = s.query(Movimiento).filter(Movimiento.user_id == uid, Movimiento.tipo.in_([tipo] if isinstance(tipo, str) else list(tipo)), Movimiento.fecha >= inicio,
                                   Movimiento.fecha < fin, Movimiento.eliminado_en.is_(None))
    for m in q.all():
        p = propio.setdefault(m.categoria_id, [Decimal(0), Decimal(0)])
        p[0] += m.monto_bs
        p[1] += m.monto_usd
    res = {cid: {"propio_bs": Decimal(0), "propio_usd": Decimal(0), "total_bs": Decimal(0), "total_usd": Decimal(0)}
           for cid in cats}
    for cid, (bs, usd) in propio.items():
        res[cid]["propio_bs"], res[cid]["propio_usd"] = bs, usd
        visto, c = set(), cats.get(cid)
        while c is not None and c.id not in visto:  # sube sumando a cada ancestro
            visto.add(c.id)
            res[c.id]["total_bs"] += bs
            res[c.id]["total_usd"] += usd
            c = cats.get(c.parent_id) if c.parent_id is not None else None
    return res


# ======== módulo: usuarios ========
"""Usuarios (máximo 2). Cada uno con sus propios datos; el nombre se registra antes del PIN."""




MAX_USUARIOS = 2
RE_NOMBRE = re.compile(r"[\w .'\-]{1,40}")


class UsuarioError(ValueError):
    pass


def nombre_valido(t):
    n = re.sub(r"\s+", " ", str(t or "")).strip()
    if not RE_NOMBRE.fullmatch(n) or not re.search(r"[^\W\d_]", n):
        raise UsuarioError("Escribe tu nombre (letras, hasta 40 caracteres).")
    return n


def buscar(s, nombre):
    return s.query(Usuario).filter(Usuario.nombre_norm == nombre_norm(nombre)).first()


def contar(s):
    return s.query(Usuario).count()


def pendiente(s):
    return s.query(Usuario).filter(Usuario.nombre.is_(None)).order_by(Usuario.id).first()


def ajustes(s, uid):
    a = s.query(Ajustes).filter(Ajustes.user_id == uid).first()
    if a is None:
        a = Ajustes(user_id=uid, saldo_inicial_usd=0)
        s.add(a)
        s.flush()
    return a


def hay_cupo(s):
    return contar(s) < MAX_USUARIOS or pendiente(s) is not None


def registrar(s, nombre):
    """Crea el usuario (o reclama los datos anteriores si hay un dueño pendiente). Valida cupo y duplicado."""
    nombre = nombre_valido(nombre)
    if buscar(s, nombre):
        raise UsuarioError("Ese nombre ya está registrado.")
    u = pendiente(s)
    if u is None:
        if contar(s) >= MAX_USUARIOS:
            raise UsuarioError("Ya hay 2 usuarios registrados en esta app.")
        u = Usuario()
        s.add(u)
    u.nombre, u.nombre_norm = nombre, nombre_norm(nombre)
    s.flush()
    sembrar(s, u.id)
    ajustes(s, u.id)
    return u


def usuarios_cambiar_nombre(s, uid, nombre):
    nombre = nombre_valido(nombre)
    otro = buscar(s, nombre)
    if otro and otro.id != uid:
        raise UsuarioError("Ese nombre ya está en uso.")
    u = s.get(Usuario, uid)
    u.nombre, u.nombre_norm = nombre, nombre_norm(nombre)
    s.flush()
    return u


# ======== módulo: migration ========
"""Migración del esquema 1 (un solo usuario, categorías de texto) al 2 (usuarios + árbol).
Todo ocurre en UNA transacción: si algo falla se revierte y se puede reintentar. Las tablas antiguas NO se borran
(quedan como respaldo) y se verifica que cuenten y sumen exactamente lo mismo."""






VERSION = 3  # 2: usuarios y árbol · 3: facturas, abonos y preferencias (solo tablas nuevas)


class MigracionError(RuntimeError):
    pass


def _tabla(conn, nombre):
    return Table(nombre, MetaData(), autoload_with=conn)


def _hay_legado(conn):
    insp = inspect(conn)
    if not insp.has_table("movimientos") and not insp.has_table("configuracion") and not insp.has_table("tasas_cambio"):
        return False
    for t in ("movimientos", "configuracion", "tasas_cambio", "presupuestos"):
        if insp.has_table(t) and conn.execute(select(_tabla(conn, t)).limit(1)).first():
            return True
    return False


def _paso_final(conn):
    """Punto de enganche para pruebas de fallo simulado (se ejecuta antes de verificar)."""


def _copiar_legado(conn):
    insp = inspect(conn)
    with Session(conn, join_transaction_mode="create_savepoint") as s:
        u = Usuario(nombre=None, nombre_norm=None)
        s.add(u)
        s.flush()
        sembrar(s, u.id)
        cats = cargar(s, u.id)
        personal = raiz_por_nombre(cats, "PERSONAL")
        por_norm = {c.nombre_norm: c.id for c in hijos_de(cats, personal.id)}

        def cat_id(texto):
            texto = (texto or "General").strip() or "General"
            n = nombre_norm(texto)
            if n not in por_norm:
                c = Categoria(user_id=u.id, parent_id=personal.id, nombre=texto[:100], nombre_norm=n[:100],
                                    orden=len(por_norm))
                s.add(c)
                s.flush()
                por_norm[n] = c.id
            return por_norm[n]

        saldo = Decimal(0)
        if insp.has_table("configuracion"):
            t = _tabla(conn, "configuracion")
            r = conn.execute(select(t).limit(1)).mappings().first()
            if r and r.get("saldo_inicial_usd") is not None:
                saldo = Decimal(r["saldo_inicial_usd"])
        s.add(Ajustes(user_id=u.id, saldo_inicial_usd=saldo))
        if insp.has_table("tasas_cambio"):
            t = _tabla(conn, "tasas_cambio")
            for r in conn.execute(select(t).order_by(t.c.fecha)).mappings():
                s.add(TasaCambio(user_id=u.id, fecha=r["fecha"], valor=r["valor"]))
        if insp.has_table("movimientos"):
            t = _tabla(conn, "movimientos")
            for r in conn.execute(select(t).order_by(t.c.id)).mappings():
                s.add(Movimiento(user_id=u.id, tipo=r["tipo"], categoria_id=cat_id(r["categoria"]),
                                 descripcion=r["descripcion"], monto_bs=r["monto_bs"], tasa_aplicada=r["tasa_aplicada"],
                                 monto_usd=r["monto_usd"], fecha=r["fecha"], creado_en=r["creado_en"]))
        if insp.has_table("presupuestos"):
            t = _tabla(conn, "presupuestos")
            vistos = set()
            for r in conn.execute(select(t).order_by(t.c.id)).mappings():
                cid = cat_id(r["categoria"])
                if cid not in vistos:
                    vistos.add(cid)
                    s.add(Presupuesto(user_id=u.id, categoria_id=cid, limite_usd=r["limite_usd"]))
        s.commit()
    _paso_final(conn)
    _verificar(conn, insp)


def _verificar(conn, insp):
    def suma(t, col):
        return sum((Decimal(x) for x in conn.execute(select(t.c[col])).scalars()), Decimal(0))
    if insp.has_table("movimientos"):
        v, n = _tabla(conn, "movimientos"), Movimiento.__table__
        c_v = conn.execute(select(v.c.id)).all()
        c_n = conn.execute(select(n.c.id)).all()
        if len(c_v) != len(c_n) or suma(v, "monto_bs") != suma(n, "monto_bs") or suma(v, "monto_usd") != suma(n, "monto_usd"):
            raise MigracionError("La verificación de movimientos no coincide; se revierte la migración.")
    if insp.has_table("tasas_cambio"):
        if conn.execute(select(_tabla(conn, "tasas_cambio").c.id)).all().__len__() != \
                conn.execute(select(TasaCambio.__table__.c.id)).all().__len__():
            raise MigracionError("La verificación de tasas no coincide; se revierte la migración.")


def migrar(conn):
    """Llamar dentro de una transacción (con el lock consultivo tomado en Postgres)."""
    legado = _hay_legado(conn)
    Base.metadata.create_all(conn)
    fila = conn.execute(select(Esquema.__table__.c.version).where(Esquema.__table__.c.id == 1)).first()
    if fila is not None and fila[0] >= VERSION:
        return False
    if legado and fila is None:   # el legado se copia UNA sola vez (las tablas antiguas quedan de respaldo)
        _copiar_legado(conn)
    conn.execute(Esquema.__table__.insert().values(id=1, version=VERSION) if fila is None
                 else Esquema.__table__.update().where(Esquema.__table__.c.id == 1).values(version=VERSION))
    return legado and fila is None


# ======== módulo: database ========
database_log = logging.getLogger("gastosbot.db")


def _preparar_sqlite(engine):
    """SQLite: transacciones reales también para DDL y claves foráneas activas (receta oficial de SQLAlchemy)."""
    @event.listens_for(engine, "connect")
    def _conectar(dbapi_conn, _):
        dbapi_conn.isolation_level = None
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")


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
        if not url.startswith("postgresql"):
            _preparar_sqlite(self.engine)
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
                    migrar(conn)
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


# ---- PIN por usuario (hash con sal; pin_hash NULL = PIN inicial) ----
ITER_PIN = 120_000


def hash_pin(pin, sal=None):
    sal = sal or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), sal, ITER_PIN)
    return f"pbkdf2${ITER_PIN}${sal.hex()}${h.hex()}"


def pin_usuario_valido(ingresado, pin_hash, pin_inicial):
    """Compara con el hash del usuario o, si no tiene, con el PIN inicial. Siempre tiempo constante."""
    if not pin_hash:
        return pin_valido(ingresado, pin_inicial)
    try:
        _, it, sal, esperado = pin_hash.split("$")
        h = hashlib.pbkdf2_hmac("sha256", str(ingresado or "").encode(), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(h.hex(), esperado)
    except (ValueError, TypeError):
        return False


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
    cat = canon_categoria(palabras[0])[:60] if palabras else None   # sin palabras: gasto PERSONAL (lo decide el chat)
    desc = " ".join(palabras[1:])[:200] or None
    return "movimiento", {"tipo": tipo, "categoria": cat, "descripcion": desc, "monto": monto,
                          "moneda": "usd" if usd else "bs", "fecha": fecha or hoy, "palabras": palabras[:12]}


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
    if (n0 == "tasa" and [x for _, x in toks[1:]] in (["sin", "cambios"], ["igual"], ["sin", "cambio"])) \
            or [x for _, x in toks] in (["sin", "cambios"], ["sin", "cambio"]):
        return "tasa_igual", {}
    if n0 == "tasa":
        fecha, rest = _extraer_fecha(toks[1:], hoy)
        nums = [t[1] for t in rest if _es_num(t[1])]
        if len(nums) != 1 or parse_monto(nums[0]) is None:
            return _parser_error("Escribe la tasa así: tasa 915 (o tasa 915 15/11/2026)")
        return "tasa", {"valor": parse_monto(nums[0]), "fecha": fecha or hoy}
    if n0 in ("nombre", "local") and not any(_es_num(x) for _, x in toks[1:]):
        resto = " ".join(o for o, _ in toks[1:])
        if not resto:
            return _parser_error("Usa: nombre Ana  o  local Centro")
        return n0, {"nombre": resto}
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
    if n0 in ("ingreso", "ingresos", "deposito", "depositos"):
        tipo, toks = "deposito", toks[1:]
    return _parser_movimiento(tipo, toks, hoy)


def generar_saludo(nombre, hora):
    h = "Buenos días" if 5 <= hora < 12 else ("Buenas tardes" if 12 <= hora < 19 else "Buenas noches")
    return random.choice([f"{h}, {nombre}. ¿En qué gastamos hoy?",
                          f"Hola {nombre}, ¿cómo va el día? Listo para anotar.",
                          f"¿Ya tomaste café, {nombre}? Cuéntame tus movimientos."])


# ======== módulo: services ========
"""Consultas y agregaciones por usuario. Todo el dinero se maneja en Decimal."""





Q2 = Decimal("0.01")
MAX_MONTO = Decimal("1000000000")
services_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def d2(x):
    return Decimal(x).quantize(Q2, rounding=ROUND_HALF_UP)


def f2(x):
    return float(d2(x))


def activos(s, uid):
    """Movimientos del usuario que no están en la papelera."""
    return s.query(Movimiento).filter(Movimiento.user_id == uid, Movimiento.eliminado_en.is_(None))


def tasa_para_fecha(s, uid, fecha):
    r = (s.query(TasaCambio).filter(TasaCambio.user_id == uid, TasaCambio.fecha <= fecha)
         .order_by(TasaCambio.fecha.desc()).first())
    return Decimal(r.valor) if r else None


def rango(mes, anio):
    inicio = date(anio, mes or 1, 1)
    fin = date(anio + 1, 1, 1) if (mes is None or mes == 12) else date(anio, mes + 1, 1)
    return inicio, fin


def movimientos(s, uid, tipo, inicio, fin):
    return (activos(s, uid).filter(Movimiento.tipo == tipo, Movimiento.fecha >= inicio, Movimiento.fecha < fin)
            .order_by(Movimiento.fecha.desc(), Movimiento.id.desc()).all())


def generar_reporte(s, uid, mes, anio, ids=None):
    gastos = movimientos(s, uid, "gasto", *rango(mes, anio))
    if ids is not None:
        gastos = [g for g in gastos if g.categoria_id in ids]
    if not gastos:
        return None
    cats = {}
    for g in gastos:
        c = cats.setdefault(g.categoria, {"total_bs": Decimal(0), "total_usd": Decimal(0), "items": []})
        c["total_bs"] += g.monto_bs
        c["total_usd"] += g.monto_usd
        c["items"].append({"id": g.id, "descripcion": g.descripcion, "fecha": g.fecha.strftime("%d/%m/%Y"),
                           "monto_bs": f2(g.monto_bs), "monto_usd": f2(g.monto_usd),
                           "tasa": float(g.tasa_aplicada), "solo_bs": g.tasa_aplicada == 0})
    total_bs = sum((g.monto_bs for g in gastos), Decimal(0))
    total_usd = sum((g.monto_usd for g in gastos), Decimal(0))
    bs_convertido = sum((g.monto_bs for g in gastos if g.tasa_aplicada > 0), Decimal(0))  # solo-Bs: tasa 0, sin USD
    orden = sorted(cats.items(), key=lambda kv: kv[1]["total_bs"], reverse=True)
    return {
        "titulo": f"Reporte de {services_MESES[mes - 1].capitalize()} {anio}" if mes else f"Resumen Anual {anio}",
        "total_bs": f2(total_bs), "total_usd": f2(total_usd),
        "tasa_promedio": f2(bs_convertido / total_usd) if total_usd else None,
        "categorias": {k: {**v, "total_bs": f2(v["total_bs"]), "total_usd": f2(v["total_usd"])} for k, v in orden},
    }


def saldo_usd(s, uid):
    cfg = s.query(Ajustes).filter(Ajustes.user_id == uid).first()
    base = Decimal(cfg.saldo_inicial_usd or 0) if cfg else Decimal(0)
    todos = activos(s, uid).all()
    ing = sum((m.monto_usd for m in todos if m.tipo in ("ingreso", "deposito")), Decimal(0))
    gas = sum((m.monto_usd for m in todos if m.tipo == "gasto"), Decimal(0))
    return base + ing - gas


def services_resumen(s, uid, hoy):
    """Datos de la pantalla principal: saldo, ahorro del mes y presupuestos."""
    todos = activos(s, uid).all()
    ini, fin = rango(hoy.month, hoy.year)
    mes = [m for m in todos if ini <= m.fecha < fin]
    ing_m = sum((m.monto_usd for m in mes if m.tipo in ("ingreso", "deposito")), Decimal(0))
    gas_m = sum((m.monto_usd for m in mes if m.tipo == "gasto"), Decimal(0))
    cats = cargar(s, uid)
    pres = []
    for p in sorted(s.query(Presupuesto).filter(Presupuesto.user_id == uid).all(), key=lambda p: p.cat.nombre_norm):
        sub = ids_subarbol(cats, p.categoria_id)
        usado = sum((m.monto_usd for m in mes if m.tipo == "gasto" and m.categoria_id in sub), Decimal(0))
        pres.append({"categoria": p.categoria, "limite_usd": f2(p.limite_usd), "gastado_usd": f2(usado)})
    onboarding = tasa_para_fecha(s, uid, hoy) is not None
    return {"onboarding_completo": onboarding, "saldo_usd": f2(saldo_usd(s, uid)), "ingresos_mes_usd": f2(ing_m),
            "gastos_mes_usd": f2(gas_m), "ahorro_mes_usd": f2(ing_m - gas_m), "presupuestos": pres}


def tasa_para_fecha_o_primera(s, uid, fecha):
    """(tasa, aproximada). Si no hay tasa <= fecha usa la primera registrada y lo marca."""
    t = tasa_para_fecha(s, uid, fecha)
    if t is not None:
        return t, False
    r = s.query(TasaCambio).filter(TasaCambio.user_id == uid).order_by(TasaCambio.fecha.asc()).first()
    return (Decimal(r.valor), True) if r else (None, False)


def fijar_presupuesto(s, uid, categoria_id, limite):
    """limite 0 elimina. Devuelve 'creado' | 'actualizado' | 'eliminado' | 'nada'."""
    p = s.query(Presupuesto).filter(Presupuesto.user_id == uid, Presupuesto.categoria_id == categoria_id).first()
    if limite == 0:
        if p:
            s.delete(p)
            return "eliminado"
        return "nada"
    if p:
        p.limite_usd = d2(limite)
        return "actualizado"
    s.add(Presupuesto(user_id=uid, categoria_id=categoria_id, limite_usd=d2(limite)))
    return "creado"


def estado_presupuesto(s, uid, categoria_id, hoy):
    """(gastado_usd, limite_usd) del mes (incluye subcategorías), o None si no tiene presupuesto."""
    p = s.query(Presupuesto).filter(Presupuesto.user_id == uid, Presupuesto.categoria_id == categoria_id).first()
    if not p:
        return None
    ini, fin = rango(hoy.month, hoy.year)
    sub = ids_subarbol(cargar(s, uid), categoria_id)
    usado = sum((m.monto_usd for m in movimientos(s, uid, "gasto", ini, fin) if m.categoria_id in sub), Decimal(0))
    return usado, Decimal(p.limite_usd)


def estado_tasa(s, uid, hoy):
    """(valor vigente | None, fecha de ese valor | None, confirmada_hoy). Vigente = la última <= hoy."""
    r = (s.query(TasaCambio).filter(TasaCambio.user_id == uid, TasaCambio.fecha <= hoy)
         .order_by(TasaCambio.fecha.desc()).first())
    if r is None:
        return None, None, False
    return Decimal(r.valor), r.fecha, r.fecha == hoy


# ======== módulo: informes ========
"""Informe jerárquico: árbol con subtotales, comparación por local (depósitos vs gastos) y «Compartido»,
con opción de repartir lo compartido en partes iguales entre los locales."""




D0 = Decimal(0)
TIPOS_DEPOSITO = ("deposito", "ingreso")


def _par(bs, usd):
    return {"bs": f2(bs), "usd": f2(usd)}


def _suma(movs, ids):
    bs = sum((m.monto_bs for m in movs if m.categoria_id in ids), D0)
    usd = sum((m.monto_usd for m in movs if m.categoria_id in ids), D0)
    return bs, usd


def repartir(valor, n):
    """Divide un Decimal (2 decimales) en n partes iguales; los centavos sobrantes se dan de uno en uno desde la primera."""
    base = (valor / n).quantize(Q2, rounding="ROUND_DOWN")
    partes = [base] * n
    resto = int(((valor - base * n) / Q2).to_integral_value())
    for i in range(resto):
        partes[i] += Q2
    return partes


def grupos(cats):
    """Conjuntos de categorías: personal, sin clasificar, locales y compartido (NEGOCIOS fuera de todo local)."""
    per, neg = raiz_por_nombre(cats, "PERSONAL"), raiz_por_nombre(cats, "NEGOCIOS")
    sin = raiz_por_nombre(cats, "SIN CLASIFICAR")
    locales = sorted((c for c in cats.values() if c.es_local), key=lambda c: (c.orden, c.nombre_norm, c.id))
    sub_loc = {c.id: ids_subarbol(cats, c.id) for c in locales}
    en_locales = set().union(*sub_loc.values()) if sub_loc else set()
    return {"personal": ids_subarbol(cats, per.id), "sin_clasificar": ids_subarbol(cats, sin.id),
            "compartido": ids_subarbol(cats, neg.id) - en_locales, "locales": locales, "sub_loc": sub_loc}


def _nodo(cats, propio, cid, excluir):
    hijos = [h for h in (_nodo(cats, propio, c.id, excluir) for c in hijos_de(cats, cid) if c.id not in excluir) if h]
    pbs, pusd = propio.get(cid, (D0, D0))
    tbs = pbs + sum((Decimal(str(h["total_bs"])) for h in hijos), D0)
    tusd = pusd + sum((Decimal(str(h["total_usd"])) for h in hijos), D0)
    if not hijos and pbs == 0 and pusd == 0 and tbs == 0:
        return None
    c = cats[cid]
    return {"id": cid, "nombre": c.nombre, "icono": c.icono, "es_local": bool(c.es_local), "propio_bs": f2(pbs),
            "propio_usd": f2(pusd), "total_bs": f2(tbs), "total_usd": f2(tusd), "hijos": hijos}


def resolver_filtro(cats, filtro):
    """None | 'personal' | 'compartido' | 'sin_clasificar' | id de local → (ids de categorías, raíz del árbol, excluir)."""
    g = grupos(cats)
    if filtro in (None, ""):
        return None, None, set()
    if filtro in ("personal", "sin_clasificar", "compartido"):
        raiz = {"personal": "PERSONAL", "sin_clasificar": "SIN CLASIFICAR", "compartido": "NEGOCIOS"}[filtro]
        excluir = set(c.id for c in g["locales"]) if filtro == "compartido" else set()
        return g[filtro], raiz_por_nombre(cats, raiz).id, excluir
    if isinstance(filtro, int) and not isinstance(filtro, bool) and filtro in g["sub_loc"]:
        return g["sub_loc"][filtro], filtro, set()
    raise ArbolError("Filtro inválido: usa un local, «personal», «compartido» o «sin_clasificar».")


def reporte(s, uid, mes, anio, filtro=None, repartir_compartido=False):
    cats = cargar(s, uid)
    ids, raiz, excluir = resolver_filtro(cats, filtro)
    ini, fin = rango(mes, anio)
    base = generar_reporte(s, uid, mes, anio, ids) or {
        "titulo": f"Reporte de {services_MESES[mes - 1].capitalize()} {anio}" if mes else f"Resumen Anual {anio}",
        "total_bs": 0.0, "total_usd": 0.0, "tasa_promedio": None, "categorias": {}}
    gastos = movimientos(s, uid, "gasto", ini, fin)
    deps = [m for t in TIPOS_DEPOSITO for m in movimientos(s, uid, t, ini, fin)]
    propio = {}
    for m in (g for g in gastos if ids is None or g.categoria_id in ids):
        p = propio.setdefault(m.categoria_id, [D0, D0])
        p[0] += m.monto_bs
        p[1] += m.monto_usd
    raices = [raiz] if raiz else [c.id for c in sorted((c for c in cats.values() if c.parent_id is None),
                                                      key=lambda c: (c.orden, c.id))]
    nodos = [n for n in (_nodo(cats, propio, r, excluir) for r in raices) if n]
    g = grupos(cats)
    tg = {k: _suma(gastos, g[k]) for k in ("personal", "sin_clasificar", "compartido")}
    td = {k: _suma(deps, g[k]) for k in ("personal", "sin_clasificar", "compartido")}
    locales = g["locales"]
    compartido_g = tg["compartido"]
    cuota_bs = repartir(compartido_g[0], len(locales)) if (repartir_compartido and locales) else None
    cuota_usd = repartir(compartido_g[1], len(locales)) if (repartir_compartido and locales) else None
    cuota_dep = repartir(td["compartido"][1], len(locales)) if (repartir_compartido and locales) else None
    por_local = []
    for i, l in enumerate(locales):
        gbs, gusd = _suma(gastos, g["sub_loc"][l.id])
        dbs, dusd = _suma(deps, g["sub_loc"][l.id])
        fila = {"local_id": l.id, "nombre": l.nombre, "icono": l.icono, "gastos": _par(gbs, gusd), "depositos": _par(dbs, dusd),
                "neto_usd": f2(dusd - gusd)}
        if cuota_bs is not None:
            fila["compartido_asignado"] = _par(cuota_bs[i], cuota_usd[i])
            fila["depositos_compartidos_asignados_usd"] = f2(cuota_dep[i])
            fila["gastos_con_compartido"] = _par(gbs + cuota_bs[i], gusd + cuota_usd[i])
            fila["neto_con_compartido_usd"] = f2(dusd + cuota_dep[i] - gusd - cuota_usd[i])
        por_local.append(fila)
    base.update({
        "arbol": nodos, "filtro": filtro if filtro not in ("",) else None, "repartido": bool(cuota_bs is not None),
        "por_local": por_local,
        "grupos": {"personal": {"gastos": _par(*tg["personal"]), "depositos": _par(*td["personal"])},
                   "compartido": {"gastos": _par(*tg["compartido"]), "depositos": _par(*td["compartido"])},
                   "sin_clasificar": {"gastos": _par(*tg["sin_clasificar"]), "depositos": _par(*td["sin_clasificar"])}},
        "depositos_total": _par(sum((m.monto_bs for m in deps if ids is None or m.categoria_id in ids), D0),
                                sum((m.monto_usd for m in deps if ids is None or m.categoria_id in ids), D0)),
    })
    return base


# ======== módulo: facturas ========
"""Facturas (solo datos), abonos y alertas de vencimiento. Todo por usuario; el dinero en Decimal.
Cada abono crea un gasto vinculado para que saldo e informes cuadren. Factura «solo Bs»: sin conversión a dólares
(el gasto se guarda con tasa 0 y USD 0, y los informes la excluyen del promedio de tasa)."""







ANIO_MIN, ANIO_MAX = 2000, 2100
DIAS_AVISO_DEFECTO = 3
MAX_CREDITO = 365


class FacturaError(ValueError):
    """Error de validación o de regla de negocio (mensaje apto para el usuario). codigo = estado HTTP sugerido."""
    def __init__(self, msg, codigo=400):
        super().__init__(msg)
        self.codigo = codigo


# ---------- validaciones ----------
def texto(valor, campo, maximo, obligatorio=True):
    t = re.sub(r"\s+", " ", str(valor if valor is not None else "")).strip()
    if not t:
        if obligatorio:
            raise FacturaError(f"Falta {campo}.")
        return None
    if len(t) > maximo or re.search(r"[<>\x00-\x1f]", t):
        raise FacturaError(f"{campo.capitalize()} inválido (máx. {maximo} caracteres, sin < >).")
    return t


def monto(valor, campo="monto"):
    if isinstance(valor, bool) or valor is None or valor == "":
        raise FacturaError(f"Falta el {campo}.")
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise FacturaError(f"El {campo} no es un número válido.")
    if not d.is_finite() or d <= 0 or d >= MAX_MONTO:
        raise FacturaError(f"El {campo} debe ser mayor que cero y razonable.")
    return d2(d)


def fecha(valor, campo="fecha"):
    if isinstance(valor, date):
        f = valor
    else:
        t = str(valor or "").strip()
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t) or None
        try:
            if m:
                f = date(int(m[1]), int(m[2]), int(m[3]))
            else:
                m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", t)
                if not m:
                    raise ValueError
                f = date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            raise FacturaError(f"La {campo} no es válida. Usa dd/mm/aaaa.")
    if not ANIO_MIN <= f.year <= ANIO_MAX:
        raise FacturaError(f"La {campo} está fuera de rango.")
    return f


def credito(valor):
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool) or not re.fullmatch(r"\d{1,3}", str(valor).strip()):
        raise FacturaError(f"Los días de crédito deben ser un entero entre 0 y {MAX_CREDITO}.")
    n = int(str(valor).strip())
    if n > MAX_CREDITO:
        raise FacturaError(f"Los días de crédito deben ser un entero entre 0 y {MAX_CREDITO}.")
    return n


def _bool(v, defecto=False):
    if v is None:
        return defecto
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("1", "true", "si", "sí"):
        return True
    if str(v).lower() in ("0", "false", "no"):
        return False
    raise FacturaError("Valor sí/no inválido.")


# ---------- preferencias ----------
def pref_get(s, uid, clave, defecto=None):
    p = s.query(Preferencia).filter(Preferencia.user_id == uid, Preferencia.clave == clave).first()
    return p.valor if p else defecto


def pref_set(s, uid, clave, valor):
    p = s.query(Preferencia).filter(Preferencia.user_id == uid, Preferencia.clave == clave).first()
    if p:
        p.valor = str(valor)
    else:
        s.add(Preferencia(user_id=uid, clave=clave, valor=str(valor)))


def preferencias(s, uid):
    return {"solo_bs_defecto": pref_get(s, uid, "solo_bs_defecto", "0") == "1",
            "dias_aviso": int(pref_get(s, uid, "dias_aviso", str(DIAS_AVISO_DEFECTO)))}


def guardar_preferencias(s, uid, datos):
    if "solo_bs_defecto" in datos:
        pref_set(s, uid, "solo_bs_defecto", "1" if _bool(datos["solo_bs_defecto"]) else "0")
    if "dias_aviso" in datos:
        n = credito(datos["dias_aviso"])
        if n is None or n > 60:
            raise FacturaError("Los días de aviso deben estar entre 0 y 60.")
        pref_set(s, uid, "dias_aviso", n)
    s.flush()
    return preferencias(s, uid)


# ---------- consultas ----------
def _propia(s, uid, fid, bloquear=False):
    q = s.query(Factura).filter(Factura.id == fid, Factura.user_id == uid, Factura.eliminado_en.is_(None))
    f = (q.with_for_update() if bloquear else q).first()
    if f is None:
        raise FacturaError("La factura no existe.", 404)
    return f


def _abonos(s, f):
    return (s.query(Abono).filter(Abono.factura_id == f.id, Abono.eliminado_en.is_(None))
            .order_by(Abono.fecha, Abono.id).all())


def vence(f):
    return f.fecha_emision + timedelta(days=f.dias_credito) if f.dias_credito is not None else None


def _estado_alerta(f, faltante, hoy, aviso):
    """None | {'tipo': 'vencida'|'por_vencer'|'sin_credito', 'dias': n} (solo si hay saldo pendiente)."""
    if faltante <= 0:
        return None
    v = vence(f)
    if v is None:
        return {"tipo": "sin_credito", "dias": None}
    dias = (v - hoy).days
    if dias < 0:
        return {"tipo": "vencida", "dias": -dias}
    if dias <= aviso:
        return {"tipo": "por_vencer", "dias": dias}
    return None


def detalle(s, cats, f, abonos, hoy, aviso, con_abonos=True):
    pagado = sum((a.monto for a in abonos), Decimal(0))
    faltante = f.monto - pagado
    v = vence(f)
    d = {"id": f.id, "numero": f.numero, "proveedor": f.proveedor, "fecha_emision": f.fecha_emision.isoformat(),
         "monto": f2(f.monto), "moneda": f.moneda, "solo_bs": bool(f.solo_bs), "dias_credito": f.dias_credito,
         "vence": v.isoformat() if v else None, "pagado": f2(pagado), "faltante": f2(faltante),
         "estado": "pagada" if faltante <= 0 else ("abonada" if pagado > 0 else "pendiente"),
         "categoria_id": f.categoria_id, "ruta": " › ".join(arbol_ruta(cats, f.categoria_id)), "nota": f.nota,
         "alerta": _estado_alerta(f, faltante, hoy, aviso)}
    if con_abonos:
        d["abonos"] = [{"id": a.id, "fecha": a.fecha.isoformat(), "monto": f2(a.monto)} for a in abonos]
    return d


def _cargar_abonos(s, uid, facturas):
    por = {f.id: [] for f in facturas}
    if facturas:
        for a in (s.query(Abono).filter(Abono.user_id == uid, Abono.eliminado_en.is_(None),
                                        Abono.factura_id.in_(list(por))).order_by(Abono.fecha, Abono.id).all()):
            por[a.factura_id].append(a)
    return por


def facturas_listar(s, uid, hoy, estado="todas", q=None):
    if estado == "papelera":
        return listar_papelera(s, uid, hoy)
    cats, aviso = cargar(s, uid), preferencias(s, uid)["dias_aviso"]
    fs = s.query(Factura).filter(Factura.user_id == uid, Factura.eliminado_en.is_(None)).all()
    if q:
        n = nombre_norm(q)
        fs = [f for f in fs if n in f.proveedor_norm or n in f.numero_norm]
    por = _cargar_abonos(s, uid, fs)
    out = [detalle(s, cats, f, por[f.id], hoy, aviso, con_abonos=False) for f in fs]
    if estado == "pendientes":
        out = [d for d in out if d["estado"] != "pagada"]
    elif estado == "pagadas":
        out = [d for d in out if d["estado"] == "pagada"]
    elif estado != "todas":
        raise FacturaError("Estado inválido (pendientes, pagadas, todas o papelera).")
    out.sort(key=lambda d: (d["estado"] == "pagada", d["vence"] is None, d["vence"] or "", d["id"]))
    return out


def obtener(s, uid, fid, hoy):
    f = _propia(s, uid, fid)
    return detalle(s, cargar(s, uid), f, _abonos(s, f), hoy, preferencias(s, uid)["dias_aviso"])


def alertas(s, uid, hoy):
    """Alertas ordenadas: vencidas (más atrasadas primero), por vencer (más próximas), sin crédito definido."""
    orden = {"vencida": 0, "por_vencer": 1, "sin_credito": 2}
    res = [{"factura_id": d["id"], "numero": d["numero"], "proveedor": d["proveedor"], "faltante": d["faltante"],
            "moneda": d["moneda"], "solo_bs": d["solo_bs"], "vence": d["vence"], "tipo": d["alerta"]["tipo"],
            "dias": d["alerta"]["dias"]} for d in facturas_listar(s, uid, hoy, "pendientes") if d["alerta"]]
    res.sort(key=lambda a: (orden[a["tipo"]], -(a["dias"] or 0) if a["tipo"] == "vencida" else (a["dias"] or 0), a["factura_id"]))
    return res


# ---------- escritura ----------
def _duplicada(s, uid, prov_norm, num_norm, excepto=None):
    q = s.query(Factura.id).filter(Factura.user_id == uid, Factura.eliminado_en.is_(None),
                                   Factura.proveedor_norm == prov_norm, Factura.numero_norm == num_norm)
    if excepto:
        q = q.filter(Factura.id != excepto)
    return q.first() is not None


def _categoria(s, uid, categoria_id):
    cats = cargar(s, uid)
    if categoria_id in (None, ""):
        return raiz_por_nombre(cats, "NEGOCIOS").id   # por defecto: gasto de negocios compartido
    if isinstance(categoria_id, bool) or not isinstance(categoria_id, int) or categoria_id not in cats:
        raise FacturaError("La categoría no existe.")
    return categoria_id


def facturas_crear(s, uid, datos, hoy):
    num = texto(datos.get("numero"), "el número", 40)
    prov = texto(datos.get("proveedor"), "el proveedor", 100)
    moneda = str(datos.get("moneda", "usd")).lower()   # lo habitual es en dólares; «solo Bs» es excepción (apagado)
    if moneda not in ("bs", "usd"):
        raise FacturaError("La moneda debe ser bs o usd.")
    solo = _bool(datos.get("solo_bs"), defecto=(moneda == "bs" and preferencias(s, uid)["solo_bs_defecto"]))
    if solo and moneda != "bs":
        raise FacturaError("«Solo Bs» aplica únicamente a facturas en bolívares.")
    pn, nn = nombre_norm(prov), nombre_norm(num)
    if _duplicada(s, uid, pn, nn):
        raise FacturaError("Ya registraste esa factura de ese proveedor.", 409)
    f = Factura(user_id=uid, numero=num, numero_norm=nn, proveedor=prov, proveedor_norm=pn,
                fecha_emision=fecha(datos.get("fecha_emision") or hoy, "fecha de emisión"),
                monto=monto(datos.get("monto")), moneda=moneda, solo_bs=solo, dias_credito=credito(datos.get("dias_credito")),
                categoria_id=_categoria(s, uid, datos.get("categoria_id")), nota=texto(datos.get("nota"), "la nota", 200, False))
    s.add(f)
    s.flush()
    return obtener(s, uid, f.id, hoy)


def facturas_editar(s, uid, fid, datos, hoy):
    f = _propia(s, uid, fid, bloquear=True)
    abonos = _abonos(s, f)
    pagado = sum((a.monto for a in abonos), Decimal(0))
    if "numero" in datos or "proveedor" in datos:
        num = texto(datos.get("numero", f.numero), "el número", 40)
        prov = texto(datos.get("proveedor", f.proveedor), "el proveedor", 100)
        pn, nn = nombre_norm(prov), nombre_norm(num)
        if _duplicada(s, uid, pn, nn, excepto=f.id):
            raise FacturaError("Ya registraste esa factura de ese proveedor.", 409)
        f.numero, f.numero_norm, f.proveedor, f.proveedor_norm = num, nn, prov, pn
    if "fecha_emision" in datos:
        nueva = fecha(datos["fecha_emision"], "fecha de emisión")
        if any(a.fecha < nueva for a in abonos):
            raise FacturaError("Hay pagos anteriores a esa fecha de emisión.")
        f.fecha_emision = nueva
    if "dias_credito" in datos:
        f.dias_credito = credito(datos["dias_credito"])
    if "monto" in datos:
        nuevo = monto(datos["monto"])
        if nuevo < pagado:
            raise FacturaError(f"El monto no puede ser menor que lo ya pagado ({f2(pagado)}).")
        f.monto = nuevo
    if "categoria_id" in datos:
        if abonos:
            raise FacturaError("Con pagos registrados la categoría no se cambia aquí: muévela desde Configuración.")
        f.categoria_id = _categoria(s, uid, datos["categoria_id"])
    if "nota" in datos:
        f.nota = texto(datos["nota"], "la nota", 200, False)
    if ("solo_bs" in datos or "moneda" in datos) and (str(datos.get("moneda", f.moneda)).lower() != f.moneda
                                                       or _bool(datos.get("solo_bs"), f.solo_bs) != bool(f.solo_bs)):
        raise FacturaError("La moneda y «solo Bs» no se cambian: borra la factura y regístrala de nuevo.")
    s.flush()
    return obtener(s, uid, f.id, hoy)


def pagar(s, uid, fid, datos, hoy, ts):
    """Abono o pago total (sin monto = paga todo lo que falta). Crea el gasto vinculado. Devuelve el detalle."""
    f = _propia(s, uid, fid, bloquear=True)
    abonos = _abonos(s, f)
    faltante = f.monto - sum((a.monto for a in abonos), Decimal(0))
    if faltante <= 0:
        raise FacturaError("Esa factura ya está pagada.", 409)
    m = faltante if datos.get("monto") in (None, "", "total") else monto(datos.get("monto"), "monto del abono")
    if m > faltante:
        raise FacturaError(f"El abono ({f2(m)}) supera lo que falta ({f2(faltante)}).")
    fp = fecha(datos.get("fecha") or hoy, "fecha del pago")
    if fp > hoy:
        raise FacturaError("No registro pagos con fecha futura.")
    if fp < f.fecha_emision:
        raise FacturaError("El pago no puede ser anterior a la emisión de la factura.")
    if f.solo_bs:
        bs, usd, tasa = m, Decimal(0), Decimal(0)               # sin recálculo a dólares
    else:
        tasa, _ = tasa_para_fecha_o_primera(s, uid, fp)
        if tasa is None:
            raise FacturaError("Primero registra la tasa del día (en el chat: tasa 915).")
        bs, usd = (d2(m * tasa), m) if f.moneda == "usd" else (m, d2(m / tasa))
        if bs <= 0:
            raise FacturaError("El monto es demasiado pequeño.")
    parcial = m < faltante or abonos
    mov = Movimiento(user_id=uid, tipo="gasto", categoria_id=f.categoria_id, monto_bs=bs, tasa_aplicada=tasa, monto_usd=usd,
                     descripcion=f"Factura {f.numero} · {f.proveedor}{' (abono)' if parcial and m < faltante else ''}"[:200],
                     fecha=fp, creado_en=ts)
    s.add(mov)
    s.flush()
    s.add(Abono(user_id=uid, factura_id=f.id, fecha=fp, monto=m, movimiento_id=mov.id, creado_en=ts))
    s.flush()
    return obtener(s, uid, f.id, hoy)


def anular_abono(s, uid, fid, aid, hoy, ts):
    f = _propia(s, uid, fid, bloquear=True)
    a = s.query(Abono).filter(Abono.id == aid, Abono.factura_id == f.id, Abono.user_id == uid,
                              Abono.eliminado_en.is_(None)).first()
    if a is None:
        raise FacturaError("El pago no existe.", 404)
    _anular(s, a, ts)
    s.flush()
    return obtener(s, uid, f.id, hoy)


def _anular(s, a, ts):
    a.eliminado_en = ts
    if a.movimiento_id:
        mov = s.get(Movimiento, a.movimiento_id)
        if mov is not None and mov.eliminado_en is None:
            mov.eliminado_en = ts


def facturas_eliminar(s, uid, fid, ts):
    """A la papelera: la factura, sus pagos y los gastos que generaron."""
    f = _propia(s, uid, fid, bloquear=True)
    for a in _abonos(s, f):
        _anular(s, a, ts)
    f.eliminado_en = ts
    s.flush()


def listar_papelera(s, uid, hoy):
    cats, aviso = cargar(s, uid), preferencias(s, uid)["dias_aviso"]
    fs = s.query(Factura).filter(Factura.user_id == uid, Factura.eliminado_en.isnot(None)).order_by(Factura.eliminado_en.desc()).all()
    out = []
    for f in fs:
        ab = (s.query(Abono).filter(Abono.factura_id == f.id, Abono.eliminado_en == f.eliminado_en).all())
        d = detalle(s, cats, f, ab, hoy, aviso, con_abonos=False)
        d["eliminada_en"] = f.eliminado_en.isoformat()
        out.append(d)
    return out


def facturas_restaurar(s, uid, fid, hoy):
    """Saca de la papelera la factura con los pagos (y gastos) que se borraron junto con ella."""
    f = s.query(Factura).filter(Factura.id == fid, Factura.user_id == uid, Factura.eliminado_en.isnot(None)).with_for_update().first()
    if f is None:
        raise FacturaError("La factura no está en la papelera.", 404)
    if _duplicada(s, uid, f.proveedor_norm, f.numero_norm, excepto=f.id):
        raise FacturaError("Ya existe otra factura con ese número de ese proveedor. Cámbiale el número a una de las dos.", 409)
    cats = cargar(s, uid)
    if f.categoria_id not in cats:
        raise FacturaError("La categoría de la factura ya no existe.", 409)
    junto = s.query(Abono).filter(Abono.factura_id == f.id, Abono.eliminado_en == f.eliminado_en).all()
    for a in junto:
        a.eliminado_en = None
        if a.movimiento_id:
            mov = s.get(Movimiento, a.movimiento_id)
            if mov is not None:
                mov.eliminado_en = None
    f.eliminado_en = None
    s.flush()
    return obtener(s, uid, f.id, hoy)


def abono_de_movimiento(s, uid, mov_id, activo=None):
    q = s.query(Abono).filter(Abono.user_id == uid, Abono.movimiento_id == mov_id)
    if activo is not None:
        q = q.filter(Abono.eliminado_en.is_(None) if activo else Abono.eliminado_en.isnot(None))
    return q.order_by(Abono.id.desc()).first()


def al_restaurar_movimiento(s, uid, mov_id):
    """Si el gasto era el pago de una factura, el pago vuelve (si cabe en el faltante y la factura sigue activa)."""
    a = abono_de_movimiento(s, uid, mov_id, activo=False)
    if a is None:
        return False
    f = s.query(Factura).filter(Factura.id == a.factura_id, Factura.user_id == uid).with_for_update().first()
    if f is None or f.eliminado_en is not None:
        raise FacturaError("Ese gasto era el pago de una factura que está en la papelera: restaura primero la factura.", 409)
    pagado = sum((x.monto for x in _abonos(s, f)), Decimal(0))
    if pagado + a.monto > f.monto:
        raise FacturaError("No se puede restaurar: ya se pagó esa parte de la factura con otros abonos.", 409)
    a.eliminado_en = None
    return True


def al_borrar_movimiento(s, uid, mov_id, ts):
    """Si el gasto borrado era el pago de una factura, el pago también se anula (el faltante se recupera)."""
    a = s.query(Abono).filter(Abono.user_id == uid, Abono.movimiento_id == mov_id, Abono.eliminado_en.is_(None)).first()
    if a is not None:
        a.eliminado_en = ts
        return True
    return False


# ======== módulo: historial ========
"""Historial de movimientos con filtros, edición limitada, papelera recuperable y borrado definitivo."""







LIMITE_MAX = 200


class HistorialError(ValueError):
    def __init__(self, msg, codigo=400):
        super().__init__(msg)
        self.codigo = codigo


def _fecha(v, campo):
    try:
        return fecha(v, campo)
    except FacturaError as e:
        raise HistorialError(str(e))


def _entero(v, campo, minimo, maximo, defecto):
    if v in (None, ""):
        return defecto
    if isinstance(v, bool) or not re.fullmatch(r"\d{1,9}", str(v)):
        raise HistorialError(f"{campo.capitalize()} inválido.")
    n = int(v)
    if not minimo <= n <= maximo:
        raise HistorialError(f"{campo.capitalize()} fuera de rango ({minimo}–{maximo}).")
    return n


def _propio(s, uid, mid, papelera):
    q = s.query(Movimiento).filter(Movimiento.id == mid, Movimiento.user_id == uid)
    q = q.filter(Movimiento.eliminado_en.isnot(None) if papelera else Movimiento.eliminado_en.is_(None))
    m = q.with_for_update().first()
    if m is None:
        raise HistorialError("El movimiento no existe." if not papelera else "El movimiento no está en la papelera.", 404)
    return m


def _item(cats, m, fac):
    loc = local_de(cats, m.categoria_id)
    return {"id": m.id, "tipo": "deposito" if m.tipo in TIPOS_DEPOSITO else "gasto", "fecha": m.fecha.isoformat(),
            "categoria_id": m.categoria_id, "categoria": m.categoria, "ruta": " › ".join(arbol_ruta(cats, m.categoria_id)),
            "local": loc.nombre if loc else None, "descripcion": m.descripcion, "monto_bs": f2(m.monto_bs),
            "monto_usd": f2(m.monto_usd), "tasa": float(m.tasa_aplicada), "solo_bs": m.tasa_aplicada == 0,
            "factura": fac.get(m.id), "eliminado_en": m.eliminado_en.isoformat() if m.eliminado_en else None}


def historial_listar(s, uid, p):
    """p: desde, hasta, categoria_id, local, tipo (gasto|deposito|todos), q, papelera (0/1), limit, offset."""
    cats = cargar(s, uid)
    q = s.query(Movimiento).filter(Movimiento.user_id == uid)
    papelera = str(p.get("papelera", "0")).lower() in ("1", "true", "si")
    q = q.filter(Movimiento.eliminado_en.isnot(None) if papelera else Movimiento.eliminado_en.is_(None))
    desde = _fecha(p["desde"], "fecha «desde»") if p.get("desde") else None
    hasta = _fecha(p["hasta"], "fecha «hasta»") if p.get("hasta") else None
    if desde and hasta and desde > hasta:
        raise HistorialError("La fecha «desde» es posterior a «hasta».")
    if desde:
        q = q.filter(Movimiento.fecha >= desde)
    if hasta:
        q = q.filter(Movimiento.fecha <= hasta)
    tipo = str(p.get("tipo") or "todos")
    if tipo == "gasto":
        q = q.filter(Movimiento.tipo == "gasto")
    elif tipo == "deposito":
        q = q.filter(Movimiento.tipo.in_(TIPOS_DEPOSITO))
    elif tipo != "todos":
        raise HistorialError("Tipo inválido (gasto, deposito o todos).")
    ids = None
    if p.get("categoria_id") not in (None, ""):
        cid = _entero(p["categoria_id"], "categoría", 1, 10**9, None)
        if cid not in cats:
            raise HistorialError("La categoría no existe.", 404)
        ids = ids_subarbol(cats, cid)
    if p.get("local") not in (None, ""):
        loc = p["local"]
        try:
            lid = int(loc) if str(loc).isdigit() else loc
            sel, _, _ = resolver_filtro(cats, lid)
        except ArbolError as e:
            raise HistorialError(str(e))
        ids = sel if ids is None else ids & sel
    if ids is not None:
        q = q.filter(Movimiento.categoria_id.in_(list(ids))) if ids else q.filter(Movimiento.id < 0)
    orden = (Movimiento.eliminado_en.desc(), Movimiento.id.desc()) if papelera else (Movimiento.fecha.desc(), Movimiento.id.desc())
    movs = q.order_by(*orden).all()
    texto = str(p.get("q") or "").strip()[:60]
    if texto:
        n = nombre_norm(texto)
        movs = [m for m in movs if n in nombre_norm(m.descripcion or "") or n in nombre_norm(m.categoria or "")]
    limit = _entero(p.get("limit"), "límite", 1, LIMITE_MAX, 50)
    offset = _entero(p.get("offset"), "desplazamiento", 0, 10**7, 0)
    pagina = movs[offset:offset + limit]
    fac = {}
    if pagina:
        for a, f in (s.query(Abono, Factura).join(Factura, Factura.id == Abono.factura_id)
                     .filter(Abono.user_id == uid, Abono.movimiento_id.in_([m.id for m in pagina])).all()):
            fac[a.movimiento_id] = {"id": f.id, "numero": f.numero, "proveedor": f.proveedor}
    gas = [m for m in movs if m.tipo not in TIPOS_DEPOSITO]
    dep = [m for m in movs if m.tipo in TIPOS_DEPOSITO]
    return {"total": len(movs), "limit": limit, "offset": offset, "items": [_item(cats, m, fac) for m in pagina],
            "suma": {"gastos_bs": f2(sum((m.monto_bs for m in gas), d2(0))),
                     "gastos_usd": f2(sum((m.monto_usd for m in gas), d2(0))),
                     "depositos_bs": f2(sum((m.monto_bs for m in dep), d2(0))),
                     "depositos_usd": f2(sum((m.monto_usd for m in dep), d2(0)))}}


def historial_editar(s, uid, mid, datos):
    """Solo categoría y descripción: el monto, la fecha y la tasa quedan congelados (se borra y se vuelve a anotar)."""
    prohibidos = {"monto", "monto_bs", "monto_usd", "tasa", "tasa_aplicada", "fecha", "tipo"} & set(datos)
    if prohibidos:
        raise HistorialError("El monto, la fecha y el tipo no se editan: borra el movimiento y vuelve a anotarlo.")
    m = _propio(s, uid, mid, papelera=False)
    cats = cargar(s, uid)
    if "categoria_id" in datos:
        c = datos["categoria_id"]
        if isinstance(c, bool) or not isinstance(c, int) or c not in cats:
            raise HistorialError("La categoría no existe.")
        m.categoria_id = c
    if "descripcion" in datos:
        d = re.sub(r"\s+", " ", str(datos["descripcion"] or "")).strip()
        if len(d) > 200 or re.search(r"[\x00-\x1f]", d):
            raise HistorialError("Descripción inválida (máx. 200 caracteres).")
        m.descripcion = d or None
    s.flush()
    s.refresh(m)
    return _item(cats, m, {})


def borrar(s, uid, mid, ts):
    m = _propio(s, uid, mid, papelera=False)
    m.eliminado_en = ts
    pago = al_borrar_movimiento(s, uid, m.id, ts)
    s.flush()
    return {"ok": True, "era_pago_de_factura": pago}


def historial_restaurar(s, uid, mid):
    m = _propio(s, uid, mid, papelera=True)
    if m.categoria_id not in cargar(s, uid):
        raise HistorialError("La categoría de ese movimiento ya no existe.", 409)
    try:
        pago = al_restaurar_movimiento(s, uid, m.id)
    except FacturaError as e:
        raise HistorialError(str(e), e.codigo)
    m.eliminado_en = None
    s.flush()
    return {"ok": True, "era_pago_de_factura": pago}


def _purgar_movs(s, uid, ids):
    if not ids:
        return 0
    for a in s.query(Abono).filter(Abono.user_id == uid, Abono.movimiento_id.in_(ids)).all():
        a.movimiento_id = None
    s.flush()
    return s.query(Movimiento).filter(Movimiento.user_id == uid, Movimiento.id.in_(ids)).delete(synchronize_session=False)


def eliminar_definitivo(s, uid, mid):
    m = _propio(s, uid, mid, papelera=True)
    _purgar_movs(s, uid, [m.id])
    return {"ok": True}


def eliminar_factura_definitivo(s, uid, fid):
    f = s.query(Factura).filter(Factura.id == fid, Factura.user_id == uid, Factura.eliminado_en.isnot(None)).with_for_update().first()
    if f is None:
        raise HistorialError("La factura no está en la papelera.", 404)
    abonos = s.query(Abono).filter(Abono.factura_id == f.id).all()
    mids = [a.movimiento_id for a in abonos if a.movimiento_id]
    for a in abonos:
        s.delete(a)
    s.flush()
    _purgar_movs(s, uid, mids)
    s.delete(f)
    s.flush()
    return {"ok": True}


def vaciar_papelera(s, uid):
    """Borra para siempre todo lo que está en la papelera (movimientos, facturas y pagos anulados)."""
    fids = [f.id for f in s.query(Factura).filter(Factura.user_id == uid, Factura.eliminado_en.isnot(None)).all()]
    n_fac = len(fids)
    for fid in fids:
        eliminar_factura_definitivo(s, uid, fid)
    anulados = s.query(Abono).filter(Abono.user_id == uid, Abono.eliminado_en.isnot(None)).all()
    mids = [a.movimiento_id for a in anulados if a.movimiento_id]
    for a in anulados:
        s.delete(a)
    s.flush()
    ids = [m.id for m in s.query(Movimiento).filter(Movimiento.user_id == uid, Movimiento.eliminado_en.isnot(None)).all()]
    n_mov = _purgar_movs(s, uid, list(set(ids) | set(mids)))
    return {"ok": True, "facturas": n_fac, "movimientos": n_mov}


# ======== módulo: panel ========
"""Soporte de la pantalla de Configuración: accesos rápidos editables y saldo inicial."""






MAX_ATAJOS = 8
ATAJOS_DEFECTO = ["Gasolina", "Comida", "Supermercado", "Servicios", "Salud"]


class PanelError(ValueError):
    pass


def _atajo(cats, c):
    ruta = arbol_ruta(cats, c.id)
    return {"categoria_id": c.id, "nombre": c.nombre, "icono": c.icono, "ruta": " › ".join(ruta),
            "prefijo": " ".join(ruta) + " "}


def atajos(s, uid):
    cats = cargar(s, uid)
    raw = pref_get(s, uid, "atajos", None)
    if raw is None:
        ids = []
        for n in ATAJOS_DEFECTO:
            h = buscar_por_nombre(cats, n)
            if h:
                ids.append(h[0].id)
    else:
        ids = [int(x) for x in raw.split(",") if x.isdigit()]
    return [_atajo(cats, cats[i]) for i in ids if i in cats]


def guardar_atajos(s, uid, ids):
    cats = cargar(s, uid)
    if not isinstance(ids, list) or len(ids) > MAX_ATAJOS:
        raise PanelError(f"Máximo {MAX_ATAJOS} accesos rápidos.")
    limpio = []
    for i in ids:
        if isinstance(i, bool) or not isinstance(i, int) or i not in cats:
            raise PanelError("Una de las categorías no existe.")
        if i not in limpio:
            limpio.append(i)
    pref_set(s, uid, "atajos", ",".join(str(i) for i in limpio))
    s.flush()
    return atajos(s, uid)


def saldo_inicial(s, uid):
    return f2(ajustes(s, uid).saldo_inicial_usd)


def guardar_saldo_inicial(s, uid, valor):
    if isinstance(valor, bool) or valor is None or valor == "":
        raise PanelError("Escribe el saldo inicial.")
    try:
        d = Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        raise PanelError("El saldo inicial no es un número válido.")
    if not d.is_finite() or abs(d) >= MAX_MONTO:
        raise PanelError("El saldo inicial es demasiado grande.")
    ajustes(s, uid).saldo_inicial_usd = d2(d)
    s.flush()
    return saldo_inicial(s, uid)


# ======== módulo: conversacion ========
"""Lógica del chat: recibe el texto, aplica reglas de negocio y devuelve (payload, http_status)."""











AYUDA = ("Puedes escribir:\n"
         "• 500 (solo el monto = gasto PERSONAL) · Gasolina 500 · Comida ayer 350 · Super $20\n"
         "• Ruta completa: negocios Centro servicio gasolina 50 12/11/2026 (fecha dd/mm/aaaa o dd-mm-aaaa; sin fecha = hoy)\n"
         "• Si la categoría tiene subcategorías o hay varias iguales, te pregunto cuál con botones\n"
         "• deposito 900 · deposito negocios Centro 500 (Bs; con $ o usd va en dólares)\n"
         "• tasa 915 · tasa 915 15/11/2026 · sin cambios (la tasa de hoy igual a la anterior)\n"
         "• reporte · septiembre 2025 · resumen del año 2026\n"
         "• presupuesto super 200 (USD/mes) · saldo · saldo inicial 1000\n"
         "• nombre Ana · local Centro (crea un local en NEGOCIOS)\n"
         "• /exportar · /deshacer (manda a la papelera el último movimiento)")
MAX_BOTONES = 10


def _t(msg, code=200, **extra):
    return {"tipo": "texto", "respuesta": msg, **extra}, code


class Ctx:
    """Quién escribe: id de usuario, su nombre y sus ajustes."""
    def __init__(self, uid, nombre, cfg):
        self.uid, self.nombre, self.cfg = uid, nombre, cfg
        self.forzado, self.texto = None, ""


def procesar(s, uid, texto, ts, forzado=None):
    """forzado = opción elegida con botones: {"id", "n", "final"} (el texto es el del movimiento pendiente)."""
    hoy = ts.date()
    cx = Ctx(uid, s.get(Usuario, uid).nombre, ajustes(s, uid))
    cx.forzado = forzado
    tasa_hoy = tasa_para_fecha(s, uid, hoy)
    if tasa_hoy is None and re.fullmatch(r"[\d.,]+", texto):
        texto = "tasa " + texto  # onboarding: basta con el número
    kind, d = interpretar(texto, hoy)
    if tasa_hoy is None and kind not in ("tasa", "tasa_igual", "ayuda", "error"):
        return _t(f"{cx.nombre}, primero necesito saber la tasa del día. Escribe por ejemplo: tasa 915")
    cx.texto = texto
    try:
        return HANDLERS[kind](s, d, ts, cx)
    except ArbolError as e:
        return _t(str(e))


def _conversacion_error(s, d, ts, cx):
    return _t(d["msg"])


def _ayuda(s, d, ts, cx):
    return _t(AYUDA)


def _exportar(s, d, ts, cx):
    return _t("📥 Descargando el respaldo CSV del mes…", tipo="descarga", url="/api/exportar")


def _tasa(s, d, ts, cx):
    v, f, hoy = d["valor"], d["fecha"], ts.date()
    if v <= 0 or v >= MAX_MONTO:
        return _t("La tasa debe ser un número mayor que cero.")
    if f > hoy:
        return _t("No puedo fijar la tasa de una fecha futura.")
    v = v.quantize(Decimal("0.0001"))
    reg = s.query(TasaCambio).filter(TasaCambio.user_id == cx.uid, TasaCambio.fecha == f).first()
    if reg:
        reg.valor = v
    else:
        s.add(TasaCambio(user_id=cx.uid, fecha=f, valor=v))
    extra = "" if f == hoy else " Los gastos ya registrados conservan su tasa."
    return _t(f"✅ Tasa del {f.strftime('%d/%m/%Y')}: Bs. {v:,.2f}.{extra} Ya puedes registrar tus gastos.")


def _saldo_inicial(s, d, ts, cx):
    if d["valor"] >= MAX_MONTO:
        return _t("Ese saldo es demasiado grande.")
    cx.cfg.saldo_inicial_usd = d2(d["valor"])
    return _t(f"✅ Saldo inicial: $ {d2(d['valor']):,.2f}")


def _saldo(s, d, ts, cx):
    r = services_resumen(s, cx.uid, ts.date())
    return _t(f"💼 Saldo disponible: **$ {r['saldo_usd']:,.2f}**\n"
              f"📈 Ahorro del mes: $ {r['ahorro_mes_usd']:,.2f}  (ingresos {r['ingresos_mes_usd']:,.2f} − gastos {r['gastos_mes_usd']:,.2f})",
              resumen=r)


def _presupuesto(s, d, ts, cx):
    if d["limite"] >= MAX_MONTO:
        return _t("Ese límite es demasiado grande.")
    cat = resolver_o_crear(s, cx.uid, d["categoria"])
    r = fijar_presupuesto(s, cx.uid, cat.id, d["limite"])
    if r == "eliminado":
        return _t(f"🗑️ Presupuesto de {cat.nombre} eliminado.")
    if r == "nada":
        return _t(f"{cat.nombre} no tenía presupuesto.")
    return _t(f"✅ Presupuesto de {cat.nombre}: $ {d2(d['limite']):,.2f} al mes.")


def _reporte(s, d, ts, cx):
    rep = reporte(s, cx.uid, d["mes"], d["anio"])
    if not rep["categorias"]:
        return _t("No encontré gastos registrados para ese período.")
    return {"tipo": "reporte", "reporte": rep}, 200


def _deshacer(s, d, ts, cx):
    m = activos(s, cx.uid).order_by(Movimiento.id.desc()).first()
    if not m:
        return _t("No hay movimientos para deshacer.")
    msg = f"↩️ Borré {m.tipo}: {m.categoria} · Bs. {m.monto_bs:,.2f} · {m.fecha.strftime('%d/%m/%Y')}"
    r = borrar(s, cx.uid, m.id, ts)   # a la papelera (recuperable)
    if r["era_pago_de_factura"]:
        msg += " (era el pago de una factura: el faltante se recuperó)"
    return _t(msg)


def _boton_cat(cats, nodo, n, final, texto=None):
    ruta = " › ".join(arbol_ruta(cats, nodo.id))
    return {"id": nodo.id, "n": n, "final": final}, {"texto": texto or f"{nodo.icono} {ruta}", "elegir": nodo.id}


def _preguntar(cats, cx, d, cabecera, opciones):
    """Respuesta con botones; el servidor recuerda el mensaje y las opciones válidas (sesión)."""
    pend, botones = [], []
    for nodo, n, final, texto in opciones[:MAX_BOTONES]:
        p, b = _boton_cat(cats, nodo, n, final, texto)
        pend.append(p)
        botones.append(b)
    extra = "\n(Hay más opciones: escribe el nombre más completo.)" if len(opciones) > MAX_BOTONES else ""
    payload, code = _t(f"{cabecera}{extra}", botones=botones)
    payload["_pend"] = {"mensaje": cx.texto, "opciones": pend}
    return payload, code


def _destino(s, cx, d):
    """('ok', categoria, palabras_usadas, nueva) o ('preguntar', payload, code)."""
    cats = cargar(s, cx.uid)
    palabras = [ALIAS.get(norm(p), p) for p in d.get("palabras", [])]
    nueva = False
    if cx.forzado:
        nodo, n, final = cats.get(cx.forzado["id"]), cx.forzado["n"], cx.forzado["final"]
        if nodo is None:
            raise ArbolError("Esa categoría ya no existe. Escribe el movimiento de nuevo.")
    elif not palabras:
        nodo, n, final = raiz_por_nombre(cats, "PERSONAL"), 0, True   # solo monto = gasto personal
    else:
        cand = resolver_ruta(cats, palabras)
        if not cand:
            nodo = arbol_crear(s, cx.uid, canon_categoria(palabras[0])[:60],
                               raiz_por_nombre(cats, "SIN CLASIFICAR").id)
            cats = cargar(s, cx.uid)
            n, final, nueva = 1, True, True
        elif len(cand) > 1:
            op = [(c, k, False, None) for c, k in cand]
            return ("preguntar",) + _preguntar(cats, cx, d, f"Hay varias «{palabras[0]}». ¿Cuál es?", op)
        else:
            (nodo, n), final = cand[0], False
    hijos = hijos_de(cats, nodo.id)
    if hijos and not final:
        ruta = " › ".join(arbol_ruta(cats, nodo.id))
        op = [(h, n, False, None) for h in hijos] + [(nodo, n, True, "📍 " + etiqueta_aqui(cats, nodo))]
        return ("preguntar",) + _preguntar(cats, cx, d, f"{nodo.icono} {ruta}: ¿en cuál lo registro?", op)
    return "ok", nodo, n, nueva


def _conversacion_movimiento(s, d, ts, cx):
    hoy, monto, fecha = ts.date(), d["monto"], d["fecha"]
    if monto <= 0 or monto >= MAX_MONTO:
        return _t("El monto debe ser mayor que cero y razonable.")
    if fecha > hoy:
        return _t("No registro movimientos con fecha futura.")
    tasa, aprox = tasa_para_fecha_o_primera(s, cx.uid, fecha)
    if tasa is None:
        return _t("Primero necesito la tasa. Escribe por ejemplo: tasa 915")
    if d["moneda"] == "usd":
        usd, bs = d2(monto), d2(monto * tasa)
    else:
        bs, usd = d2(monto), d2(monto / tasa)
    if bs <= 0:
        return _t("El monto es demasiado pequeño.")
    res = _destino(s, cx, d)
    if res[0] == "preguntar":
        return res[1], res[2]
    _, cat, usadas, nueva = res
    desc = " ".join(d.get("palabras", [])[usadas:])[:200] or None
    m = Movimiento(user_id=cx.uid, tipo=d["tipo"], categoria_id=cat.id, descripcion=desc, monto_bs=bs,
                   tasa_aplicada=tasa, monto_usd=usd, fecha=fecha, creado_en=ts)
    s.add(m)
    s.flush()
    m.cat = cat
    cats = cargar(s, cx.uid)
    ruta = " › ".join(arbol_ruta(cats, cat.id))
    gasto = d["tipo"] == "gasto"
    et = "registrado" if gasto else "depósito registrado"
    lineas = [f"{'✅' if gasto else '💰'} **{cat.nombre}** {et}{' — ' + desc if desc else ''}"]
    if ruta != cat.nombre:
        lineas.append(f"📂 {ruta}")
    lineas += [f"📅 Fecha: {fecha.strftime('%d/%m/%Y')}", f"💵 Bs. {bs:,.2f}  |  **$ {usd:,.2f}** (Tasa: {tasa:,.2f})"]
    if nueva:
        lineas.append(f"🆕 «{cat.nombre}» no existía: la guardé en SIN CLASIFICAR (muévela en Configuración).")
    if aprox:
        lineas.append(f"⚠️ No había tasa anterior al {fecha.strftime('%d/%m/%Y')}; usé la primera registrada. "
                      f"Para fijarla: tasa X {fecha.strftime('%d/%m/%Y')} y vuelve a anotarlo.")
    extra = {}
    vig, fvig, confirmada = estado_tasa(s, cx.uid, hoy)
    if fecha == hoy and not confirmada and vig is not None:
        lineas.append(f"⚠️ Usé la tasa del {fvig.strftime('%d/%m/%Y')} (Bs. {vig:,.2f}): aún no confirmas la de hoy.")
        extra["botones"] = botones_tasa(vig)
    if gasto:
        est = estado_presupuesto(s, cx.uid, cat.id, hoy)
        if est and est[1] > 0:
            usado, lim = est
            pct = int(usado / lim * 100)
            if pct >= 100:
                lineas.append(f"🚨 Superaste el presupuesto de {cat.nombre}: $ {usado:,.2f} / $ {lim:,.2f}")
            elif pct >= 80:
                lineas.append(f"⚠️ Vas al {pct}% del presupuesto de {cat.nombre}: $ {usado:,.2f} / $ {lim:,.2f}")
    saldo = saldo_usd(s, cx.uid)
    lineas.append(f"💼 Saldo estimado: **$ {d2(saldo):,.2f}**")
    mov = {"id": m.id, "tipo": m.tipo, "categoria": cat.nombre, "ruta": ruta, "descripcion": desc,
           "monto_bs": float(bs), "monto_usd": float(usd), "tasa": float(tasa), "saldo_usd": float(d2(saldo)),
           "fecha": fecha.isoformat(), "hora": ts.strftime("%H:%M")}
    return _t("\n".join(lineas), movimiento=mov, **extra)


def botones_tasa(valor):
    return [{"texto": f"Sin cambios ({valor:,.2f})", "mensaje": "tasa sin cambios"},
            {"texto": "Escribir otra", "prefijo": "tasa "}]


def _tasa_igual(s, d, ts, cx):
    hoy = ts.date()
    vig, fvig, confirmada = estado_tasa(s, cx.uid, hoy)
    if vig is None:
        return _t("Aún no hay una tasa anterior. Escribe por ejemplo: tasa 915")
    if confirmada:
        return _t(f"Ya tienes la tasa de hoy: Bs. {vig:,.2f}. Para cambiarla escribe: tasa 915")
    s.add(TasaCambio(user_id=cx.uid, fecha=hoy, valor=vig))
    return _t(f"✅ Tasa de hoy sin cambios: Bs. {vig:,.2f} (la del {fvig.strftime('%d/%m/%Y')}). Ya puedes registrar tus gastos.")


def _nombre(s, d, ts, cx):
    try:
        n = d["nombre"].strip()
        u = usuarios_cambiar_nombre(s, cx.uid, n.title() if n.islower() or n.isupper() else n)
    except UsuarioError as e:
        return _t(str(e))
    return _t(f"✅ Listo, ahora te llamo {u.nombre}.")


def _local(s, d, ts, cx):
    neg = raiz_por_nombre(cargar(s, cx.uid), "NEGOCIOS")
    c = arbol_crear(s, cx.uid, d["nombre"], neg.id, "🏬", es_local=True)
    return _t(f"🏬 Local «{c.nombre}» creado en NEGOCIOS. Ahora puedes escribir, por ejemplo: negocios {c.nombre} servicio luz 50")


HANDLERS = {"error": _conversacion_error, "ayuda": _ayuda, "exportar": _exportar, "tasa": _tasa, "saldo": _saldo,
            "saldo_inicial": _saldo_inicial, "presupuesto": _presupuesto, "reporte": _reporte,
            "deshacer": _deshacer, "movimiento": _conversacion_movimiento, "tasa_igual": _tasa_igual, "nombre": _nombre,
            "local": _local}


# ======== módulo: assets ========
"""Archivos de la interfaz (HTML, CSS, JS, service worker, manifest, íconos).
En desarrollo se leen de disco; en el archivo único generado (tools/build_single.py)
el bloque marcado se reemplaza por los mismos bytes incrustados."""



ASSETS = {
    'index.html': '<!DOCTYPE html>\n<html lang="es">\n<head>\n  <meta charset="UTF-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />\n  <meta name="theme-color" content="#0F172A" />\n  <meta name="apple-mobile-web-app-capable" content="yes" />\n  <meta name="mobile-web-app-capable" content="yes" />\n  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />\n  <meta name="apple-mobile-web-app-title" content="GastosBot" />\n  <link rel="manifest" href="/static/manifest.json" />\n  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png" />\n  <link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png" />\n  <link rel="stylesheet" href="/static/app.css" />\n  <title>GastosBot</title>\n</head>\n<body>\n  <!-- Bloqueo por PIN -->\n  <main id="lock" class="lock" aria-labelledby="lock-title">\n    <div class="glass lock-card">\n      <div class="lock-emoji" aria-hidden="true">🔒</div>\n      <h1 id="lock-title">GastosBot</h1>\n      <p class="muted">Escribe tu nombre y tu PIN para continuar</p>\n      <input id="nombre" type="text" maxlength="40" autocomplete="username" autocapitalize="words"\n             placeholder="Tu nombre" aria-label="Nombre" />\n      <input id="pin" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="off"\n             placeholder="••••" aria-label="PIN" />\n      <button id="pin-btn" class="btn" type="button">Entrar</button>\n      <p id="pin-error" class="error" role="alert" hidden></p>\n    </div>\n  </main>\n\n  <!-- App -->\n  <div id="app" class="app" hidden>\n    <header class="glass topbar">\n      <button id="btn-config" class="icon-btn" type="button" aria-label="Configuración" title="Configuración">⚙️</button>\n      <h1 class="title">GastosBot</h1>\n      <div class="top-btns">\n        <button id="btn-alertas" class="icon-btn" type="button" aria-label="Alertas" title="Alertas">🔔<span id="alerta-badge" class="badge-alerta" hidden>!</span></button>\n        <button id="btn-facturas" class="icon-btn" type="button" aria-label="Facturas" title="Facturas">🧾</button>\n        <button id="btn-historial" class="icon-btn" type="button" aria-label="Historial" title="Historial">📜</button>\n        <button id="btn-reporte" class="icon-btn" type="button" aria-label="Reporte del mes" title="Reporte del mes">📊</button>\n      </div>\n    </header>\n\n    <section id="chat" class="chat" aria-live="polite" aria-label="Conversación">\n      <div id="resumen" class="resumen" hidden></div>\n    </section>\n\n    <footer class="bottom">\n      <div class="chips" id="chips" role="group" aria-label="Accesos rápidos">\n        <button class="chip" type="button" data-prefijo="Gasolina ">⛽ Gasolina</button>\n        <button class="chip" type="button" data-prefijo="Comida ">🍔 Comida</button>\n        <button class="chip" type="button" data-prefijo="Super ">🛒 Super</button>\n        <button class="chip" type="button" data-prefijo="Servicios ">💡 Servicios</button>\n        <button class="chip" type="button" data-prefijo="Salud ">💊 Salud</button>\n      </div>\n      <form id="form" class="glass composer" autocomplete="off">\n        <input id="texto" type="text" enterkeyhint="send" autocomplete="off" autocapitalize="sentences"\n               maxlength="300" placeholder="Escribe tu mensaje…" aria-label="Mensaje" />\n        <button id="enviar" class="send" type="submit" aria-label="Enviar">➤</button>\n      </form>\n    </footer>\n  </div>\n\n  <script src="/static/app.js"></script>\n</body>\n</html>\n'.encode('utf-8'),
    'static/app.css': ':root {\n  --mint: #34D399; --mint-soft: rgba(52, 211, 153, .18); --mint-line: rgba(52, 211, 153, .55);\n  --text: #F8FAFC; --muted: #94A3B8; --red: #F87171; --amber: #FBBF24;\n  --glass: rgba(255, 255, 255, .08); --glass-border: rgba(255, 255, 255, .16);\n  --app-h: 100dvh; --app-top: 0px;\n}\n* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent;\n    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif; }\nhtml, body { height: 100%; overflow: hidden; }\nbody { position: fixed; inset: 0; width: 100%; color: var(--text);\n       background: linear-gradient(160deg, #0F172A 0%, #1E1B4B 100%) fixed; }\n[hidden] { display: none !important; }\nbutton { cursor: pointer; color: inherit; border: 0; background: none; font: inherit; }\n.muted { color: var(--muted); font-size: .9rem; }\n.error { color: var(--red); font-size: .9rem; margin-top: 10px; }\n\n.glass { background: var(--glass); border: 1px solid var(--glass-border);\n         -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);\n         box-shadow: 0 8px 28px rgba(0, 0, 0, .25), inset 0 1px 0 rgba(255, 255, 255, .08); }\n@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {\n  .glass { background: rgba(30, 41, 80, .85); }\n}\n\n/* PIN */\n.lock { position: fixed; inset: 0; display: grid; place-items: center; padding: 24px; }\n.lock-card { width: min(340px, 100%); border-radius: 28px; padding: 28px 24px; text-align: center; }\n.lock-card h1 { margin: 6px 0 4px; font-size: 1.5rem; }\n.lock-emoji { font-size: 2.2rem; }\n#pin { width: 100%; margin: 18px 0 12px; padding: 14px; text-align: center; letter-spacing: .5em; font-size: 1.4rem;\n       color: var(--text); background: rgba(255, 255, 255, .07); border: 1px solid var(--glass-border); border-radius: 16px; outline: none; }\n#pin:focus { border-color: var(--mint-line); }\n.btn { width: 100%; padding: 13px; border-radius: 16px; font-weight: 600; color: #052e22; background: var(--mint); }\n.btn:disabled { opacity: .5; }\n\n/* Layout */\n.app { position: fixed; left: 0; right: 0; top: var(--app-top); height: var(--app-h);\n       display: flex; flex-direction: column; max-width: 720px; margin: 0 auto; }\n.topbar { display: flex; align-items: center; justify-content: space-between; z-index: 5;\n          margin: calc(8px + env(safe-area-inset-top)) 12px 0; padding: 8px 10px; border-radius: 22px; }\n.title { font-size: 1.1rem; font-weight: 650; text-align: center; letter-spacing: .02em; }\n.spacer, .icon-btn { width: 40px; height: 36px; }\n.icon-btn { font-size: 1.3rem; border-radius: 12px; background: var(--mint-soft); border: 1px solid var(--mint-line); }\n.icon-btn:active { transform: scale(.94); }\n.chat { flex: 1; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch;\n        padding: 14px 12px 8px; display: flex; flex-direction: column; gap: 10px; }\n\n/* Resumen */\n.resumen { display: flex; flex-direction: column; gap: 10px; }\n.saldo { display: flex; align-items: center; gap: 16px; padding: 16px 18px; border-radius: 26px; }\n.ring { flex: none; width: 74px; height: 74px; }\n.ring circle { fill: none; stroke-width: 7; }\n.ring .track { stroke: rgba(255, 255, 255, .14); }\n.ring .prog { stroke: var(--mint); stroke-linecap: round; transform: rotate(-90deg); transform-origin: 50% 50%;\n              transition: stroke-dashoffset .6s ease; filter: drop-shadow(0 0 4px rgba(52, 211, 153, .6)); }\n.saldo .label { color: var(--muted); font-size: .85rem; }\n.saldo .monto { color: var(--mint); font-size: 1.9rem; font-weight: 700; line-height: 1.15; }\n.saldo .sub { font-size: .85rem; margin-top: 2px; }\n.presu { padding: 10px 14px; border-radius: 20px; }\n.presu .fila { display: flex; justify-content: space-between; gap: 8px; font-size: .92rem; }\n.presu .nombre { color: var(--mint); }\n.bar { height: 6px; margin-top: 7px; border-radius: 6px; background: rgba(255, 255, 255, .14); overflow: hidden; }\n.bar > i { display: block; height: 100%; border-radius: 6px; background: var(--mint); transition: width .5s ease; }\n.bar.warn > i { background: var(--amber); } .bar.over > i { background: var(--red); }\n\n/* Burbujas */\n.msg { max-width: 86%; padding: 10px 14px; border-radius: 20px; line-height: 1.38; white-space: pre-wrap;\n       overflow-wrap: anywhere; animation: pop .22s ease both; }\n.msg.user { align-self: flex-end; background: var(--mint-soft); border: 1px solid var(--mint-line); border-bottom-right-radius: 6px; }\n.msg.bot { align-self: flex-start; border-bottom-left-radius: 6px; }\n.msg.err { border-color: rgba(248, 113, 113, .5); }\n.msg strong { color: var(--mint); font-weight: 650; }\n.retry { display: block; margin-top: 8px; padding: 6px 14px; border-radius: 12px; background: var(--mint-soft); border: 1px solid var(--mint-line); }\n.mov { display: flex; gap: 12px; align-items: center; white-space: normal; min-width: 62%; }\n.mov .emoji { font-size: 1.6rem; }\n.mov .cuerpo { flex: 1; min-width: 0; }\n.mov .cat { color: var(--mint); font-size: 1.05rem; }\n.mov .desc, .mov .meta { color: var(--muted); font-size: .8rem; }\n.mov .der { text-align: right; }\n.mov .usd { font-weight: 650; } .mov .usd.pos { color: var(--mint); }\n.mov .nota { flex-basis: 100%; font-size: .82rem; color: var(--amber); }\n.mov-wrap { flex-wrap: wrap; }\n.typing span { display: inline-block; width: 7px; height: 7px; margin: 0 2px; border-radius: 50%; background: var(--muted); animation: blink 1.1s infinite; }\n.typing span:nth-child(2) { animation-delay: .18s; } .typing span:nth-child(3) { animation-delay: .36s; }\n\n/* Reporte */\n.reporte { align-self: stretch; display: flex; flex-direction: column; gap: 8px; animation: pop .25s ease both; }\n.rep-card { border-radius: 26px; padding: 14px 16px 16px; text-align: center; }\n.rep-card .rt { display: inline-block; padding: 5px 16px; border-radius: 12px; background: var(--glass); border: 1px solid var(--glass-border); font-size: .95rem; }\n.rep-card .tot { color: var(--mint); font-size: 1.55rem; font-weight: 700; margin-top: 8px; line-height: 1.25; }\n.rep-card .prom { color: var(--muted); font-size: .85rem; margin: 4px 0 8px; }\n.donut-row { display: flex; align-items: center; justify-content: center; gap: 16px; flex-wrap: wrap; }\n.donut { width: 130px; height: 130px; transform: rotate(-90deg); }\n.donut circle { fill: none; stroke-width: 6; }\n.leyenda { text-align: left; font-size: .8rem; display: grid; gap: 3px; }\n.leyenda i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }\n.pill { width: 100%; text-align: left; border-radius: 22px; padding: 0; background: rgba(52, 211, 153, .07); border: 1.5px solid var(--mint-line);\n        box-shadow: 0 0 14px rgba(52, 211, 153, .15); overflow: hidden; }\n.pill-head { display: flex; align-items: center; gap: 8px; width: 100%; padding: 12px 16px; }\n.pill-head .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.pill-head .chev { transition: transform .3s ease; color: var(--muted); }\n.pill.open { background: rgba(52, 211, 153, .14); }\n.pill.open .chev { transform: rotate(180deg); }\n.panel { display: grid; grid-template-rows: 0fr; transition: grid-template-rows .32s ease; }\n.pill.open .panel { grid-template-rows: 1fr; }\n.panel-inner { overflow: hidden; }\n.items { margin: 0 12px 12px; padding: 4px 12px; border-radius: 16px; background: rgba(15, 23, 42, .35); border: 1px solid var(--glass-border); }\n.item { display: flex; gap: 8px; padding: 8px 0; font-size: .88rem; border-bottom: 1px solid rgba(255, 255, 255, .08); }\n.item:last-child { border-bottom: 0; }\n.item .f { color: var(--muted); flex: none; }\n.item .d { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.item .bs { color: var(--red); flex: none; } .item .u { color: var(--muted); flex: none; }\n\n/* Zona inferior */\n.bottom { padding: 6px 12px calc(10px + env(safe-area-inset-bottom)); }\n.chips { display: flex; gap: 8px; overflow-x: auto; padding: 4px 2px 10px; scrollbar-width: none; }\n.chips::-webkit-scrollbar { display: none; }\n.chip { flex: none; padding: 8px 14px; border-radius: 999px; font-size: .92rem; background: var(--glass); border: 1px solid var(--glass-border);\n        -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); }\n.chip:active { background: var(--mint-soft); }\n.composer { display: flex; align-items: center; gap: 8px; padding: 6px 6px 6px 16px; border-radius: 999px; }\n#texto { flex: 1; min-width: 0; font-size: 16px; color: var(--text); background: none; border: 0; outline: none; padding: 8px 0; }\n#texto::placeholder { color: var(--muted); }\n.send { width: 40px; height: 40px; border-radius: 50%; background: var(--mint); color: #052e22; font-size: 1.05rem; }\n.send:disabled { opacity: .45; }\n:focus-visible { outline: 2px solid var(--mint); outline-offset: 2px; }\n\n@keyframes pop { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }\n@keyframes blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }\n@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }\n\n.opciones { display: flex; flex-wrap: wrap; gap: 8px; margin: 2px 0 10px; }\n.opcion { padding: 8px 14px; border-radius: 14px; background: var(--mint-soft); border: 1px solid var(--mint-line); text-align: left; max-width: 100%; }\n\n/* ---- paneles ---- */\n.top-btns { display: flex; gap: 6px; }\n.icon-btn { position: relative; }\n.badge-alerta { position: absolute; top: -6px; right: -6px; min-width: 18px; height: 18px; padding: 0 4px; border-radius: 9px; background: var(--red); color: #fff;\n                font-size: .72rem; font-weight: 800; line-height: 18px; text-align: center; }\n.title { font-size: 1rem; }\n.p-full { position: fixed; inset: 0; z-index: 20; display: flex; flex-direction: column; max-width: 720px; margin: 0 auto;\n          padding-top: env(safe-area-inset-top); background: linear-gradient(160deg, #0F172A 0%, #1E1B4B 100%); }\n.p-head { display: flex; align-items: center; gap: 10px; margin: 8px 12px 0; padding: 8px 12px; border-radius: 20px; }\n.p-t { font-size: 1.05rem; font-weight: 650; }\n.p-x { width: 38px; height: 34px; border-radius: 12px; font-size: 1.2rem; background: var(--mint-soft); border: 1px solid var(--mint-line); }\n.p-body { flex: 1; overflow-y: auto; padding: 12px 12px calc(24px + env(safe-area-inset-bottom)); display: flex; flex-direction: column; gap: 12px; -webkit-overflow-scrolling: touch; }\n.p-aviso { margin: 8px 12px 0; padding: 10px 12px; border-radius: 12px; font-size: .9rem; color: #fecaca; background: rgba(248, 113, 113, .15); border: 1px solid rgba(248, 113, 113, .4); }\n.p-aviso.ok { color: #a7f3d0; background: var(--mint-soft); border-color: var(--mint-line); }\n.sec { border-radius: 20px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }\n.sec h3 { font-size: .95rem; letter-spacing: .02em; }\n.kv { display: flex; justify-content: space-between; gap: 12px; font-size: .92rem; }\n.kv span:last-child { text-align: right; word-break: break-word; }\n.campo { display: flex; flex-direction: column; gap: 6px; font-size: .9rem; }\n.campo.check { flex-direction: row; flex-wrap: wrap; align-items: center; gap: 10px; }\n.campo.check small { flex-basis: 100%; }\n.campo.check input { width: 22px; height: 22px; accent-color: var(--mint); }\ninput[type=text], input[type=date], input[type=password], select, textarea {\n  width: 100%; padding: 11px 12px; color: var(--text); font-size: 1rem; background: rgba(255, 255, 255, .07); border: 1px solid var(--glass-border); border-radius: 12px; outline: none; }\nselect option { color: #0F172A; }\ninput:focus, select:focus, textarea:focus { border-color: var(--mint-line); }\n.fila-inp { display: flex; gap: 8px; align-items: center; }\n.fila-inp > input, .fila-inp > select { flex: 1; min-width: 0; }\n.fila-btns { display: flex; gap: 8px; flex-wrap: wrap; }\n.btn-s { padding: 10px 14px; border-radius: 12px; font-weight: 600; font-size: .92rem; background: var(--glass); border: 1px solid var(--glass-border); }\n.btn-s.prim { color: #052e22; background: var(--mint); border-color: var(--mint); }\n.btn-s.peligro { color: #fecaca; border-color: rgba(248, 113, 113, .5); background: rgba(248, 113, 113, .12); }\n.btn-s.prim.peligro { color: #fff; background: #dc2626; border-color: #dc2626; }\n.btn-s.lista { width: 100%; text-align: left; }\n.btn-s:disabled, .mas:disabled { opacity: .4; }\n.salir { align-self: stretch; }\n.arbol { display: flex; flex-direction: column; gap: 2px; }\n.nodo, .atajo, .inf-nodo, .abono { display: flex; align-items: center; gap: 8px; padding: 8px; border-radius: 10px; background: rgba(255, 255, 255, .04); font-size: .92rem; }\n.nodo .nm, .inf-nodo .nm { flex: 1; min-width: 0; word-break: break-word; }\n.inf-nodo .mo { font-size: .85rem; color: var(--muted); white-space: nowrap; }\n.atajo span { flex: 1; word-break: break-word; }\n.abono span { flex: 1; }\n.mas { min-width: 34px; height: 32px; border-radius: 10px; background: var(--glass); border: 1px solid var(--glass-border); }\n.tag { display: inline-block; margin: 2px 4px 0 0; padding: 2px 8px; border-radius: 999px; font-size: .72rem; font-weight: 600; background: var(--glass); border: 1px solid var(--glass-border); }\n.tag.ok { color: #a7f3d0; background: var(--mint-soft); border-color: var(--mint-line); }\n.tag.aviso { color: #fde68a; background: rgba(251, 191, 36, .14); border-color: rgba(251, 191, 36, .5); }\n.tag.mal { color: #fecaca; background: rgba(248, 113, 113, .16); border-color: rgba(248, 113, 113, .5); }\n.lista { display: flex; flex-direction: column; gap: 8px; }\n.vacio { text-align: center; padding: 18px 8px; }\n.item-h { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; text-align: left; border-radius: 16px; background: var(--glass); border: 1px solid var(--glass-border); }\n.item-h .ic, .nodo .ic { font-size: 1.4rem; }\n.item-h .cu, .alerta .cu { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }\n.t1 { font-weight: 600; word-break: break-word; }\n.t2 { font-size: .8rem; word-break: break-word; }\n.item-h .mo { display: flex; flex-direction: column; align-items: flex-end; font-weight: 650; white-space: nowrap; }\n.item-h .mo.pos { color: var(--mint); }\n.item-h .mo small { font-weight: 400; font-size: .72rem; }\n.filtros { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 12px; border-radius: 18px; }\n.filtros > select:first-child, .filtros > .btn-s, .filtros > .campo, .filtros input[type=text] { grid-column: span 1; }\n.resu { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; font-size: .85rem; color: var(--muted); }\n.pie { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }\n.tabs { display: flex; gap: 6px; }\n.tab { flex: 1; padding: 9px; border-radius: 12px; font-weight: 600; font-size: .9rem; background: var(--glass); border: 1px solid var(--glass-border); }\n.tab.on { color: #052e22; background: var(--mint); border-color: var(--mint); }\n.alerta { display: flex; flex-direction: column; gap: 8px; padding: 12px; border-radius: 16px; background: var(--glass); border: 1px solid var(--glass-border); }\n.alerta.mal { border-color: rgba(248, 113, 113, .55); }\n.alerta.aviso { border-color: rgba(251, 191, 36, .55); }\n.local-card { display: flex; flex-direction: column; gap: 4px; padding: 10px; border-radius: 12px; background: rgba(255, 255, 255, .04); }\n.informe { display: flex; flex-direction: column; gap: 12px; }\n/* diálogos */\n.modal { position: fixed; inset: 0; z-index: 40; display: grid; place-items: center; padding: 16px; background: rgba(2, 6, 23, .7); overflow-y: auto; }\n.modal-card { width: min(420px, 100%); max-height: 90dvh; overflow-y: auto; border-radius: 22px; padding: 18px; display: flex; flex-direction: column; gap: 12px; background: rgba(15, 23, 42, .96); }\n.modal-card h3 { font-size: 1.05rem; }\n.modal-btns { display: flex; gap: 8px; justify-content: flex-end; }\n.icono-actual { width: 52px; height: 46px; font-size: 1.6rem; border-radius: 12px; background: var(--glass); border: 1px solid var(--glass-border); }\n.icono-grid { margin-top: 8px; max-height: 220px; overflow-y: auto; padding: 8px; border-radius: 12px; background: rgba(255, 255, 255, .05); }\n.icono-g { margin: 6px 0 2px; font-size: .75rem; color: var(--muted); }\n.icono-fila { display: flex; flex-wrap: wrap; gap: 4px; }\n.icono-op { width: 40px; height: 38px; font-size: 1.35rem; border-radius: 10px; }\n.icono-op:active, .icono-op:hover { background: var(--mint-soft); }\n.reporte > .btn-s { margin-top: 8px; }\n'.encode('utf-8'),
    'static/app.js': '/* GastosBot — frontend. Todo el texto dinámico se inserta con DOM (textContent), nunca con innerHTML. */\n(() => {\n  "use strict";\n  const $ = (id) => document.getElementById(id);\n  const NS = "http://www.w3.org/2000/svg";\n  const fmt = (n) => Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });\n  const norm = (t) => String(t).normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();\n  const state = { busy: false, onboarded: false, nombre: "", arbol: [] };\n\n  function h(tag, props, ...kids) {\n    const el = document.createElement(tag);\n    for (const [k, v] of Object.entries(props || {})) {\n      if (v == null || v === false) continue;\n      if (k === "class") el.className = v;\n      else if (k === "text") el.textContent = v;\n      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);\n      else el.setAttribute(k, v);\n    }\n    for (const kid of kids.flat()) if (kid != null) el.append(kid);\n    return el;\n  }\n  function svg(tag, attrs) {\n    const el = document.createElementNS(NS, tag);\n    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);\n    return el;\n  }\n  function rich(texto) { // **negrita** -> <strong> (seguro)\n    const frag = document.createDocumentFragment();\n    String(texto).split(/\\*\\*(.+?)\\*\\*/g).forEach((p, i) => {\n      if (p) frag.append(i % 2 ? h("strong", { text: p }) : document.createTextNode(p));\n    });\n    return frag;\n  }\n\n  const ICONOS = { gasolina: "⛽", comida: "🍔", supermercado: "🛒", servicios: "💡", salud: "💊", cafe: "☕",\n    taxi: "🚕", uber: "🚕", transporte: "🚌", entretenimiento: "🎉", restaurante: "🍽️", sueldo: "💰", ingreso: "💰" };\n  const icono = (cat) => ICONOS[norm(cat)] || "🧾";\n\n  /* ---------- red ---------- */\n  class ApiError extends Error {\n    constructor(kind, message, status) { super(message); this.kind = kind; this.status = status || 0; }\n  }\n  async function api(path, { method = "GET", body, timeout = 15000 } = {}) {\n    if (typeof navigator !== "undefined" && navigator.onLine === false)\n      throw new ApiError("offline", "Sin conexión. Revisa tu internet e inténtalo de nuevo.");\n    const ctl = new AbortController();\n    const timer = setTimeout(() => ctl.abort(), timeout);\n    try {\n      return await fetch(path, { method, credentials: "same-origin", signal: ctl.signal,\n        headers: (body || method !== "GET") ? { "Content-Type": "application/json" } : {}, body: (body || method !== "GET") ? JSON.stringify(body || {}) : undefined });\n    } catch (e) {\n      if (e && e.name === "AbortError") throw new ApiError("timeout", "El servidor tardó demasiado en responder.");\n      throw new ApiError("network", "No pude conectar con el servidor.");\n    } finally { clearTimeout(timer); }\n  }\n  async function apiJson(path, opts) {\n    const res = await api(path, opts);\n    let data = null;\n    try { data = await res.json(); } catch (_) { /* no era JSON */ }\n    if (!res.ok) {\n      if (res.status === 401 && path !== "/api/auth") { bloquear("Tu sesión expiró. Ingresa el PIN de nuevo."); }\n      const msg = (data && data.error) || (res.status >= 500 ? "El servidor tuvo un problema. Intenta de nuevo." : `Error inesperado (${res.status}).`);\n      throw new ApiError(res.status === 503 ? "unavailable" : "http", msg, res.status);\n    }\n    if (data === null || typeof data !== "object") throw new ApiError("badjson", "Respuesta inválida del servidor.");\n    return data;\n  }\n\n  /* ---------- chat ---------- */\n  const chat = () => $("chat");\n  function bajar() {\n    const c = chat();\n    requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });\n  }\n  function agregar(el, { top = false } = {}) {\n    chat().append(el);\n    if (top && el.scrollIntoView) requestAnimationFrame(() => el.scrollIntoView({ block: "start", behavior: "smooth" }));\n    else bajar();\n    return el;\n  }\n  const burbujaUsuario = (t) => agregar(h("div", { class: "msg user", text: t }));\n  const burbujaBot = (t) => agregar(h("div", { class: "msg bot glass" }, rich(t)));\n  function burbujaError(e, reintentar) {\n    let msg = e.message || "Ocurrió un error.";\n    if (reintentar && (e.kind === "timeout" || e.kind === "network"))\n      msg += " Si era un gasto, revisa con «reporte» antes de reintentar para no duplicarlo.";\n    const b = h("div", { class: "msg bot glass err", role: "alert" }, rich("⚠️ " + msg));\n    if (reintentar) b.append(h("button", { class: "retry", type: "button", text: "Reintentar",\n      onclick: () => { b.remove(); reintentar(); } }));\n    return agregar(b);\n  }\n  /* Botones que el bot ofrece (elegir categoría, confirmar tasa). Al pulsar uno se retiran todos. */\n  function botonesBot(botones) {\n    if (!Array.isArray(botones) || !botones.length) return null;\n    const caja = h("div", { class: "opciones", role: "group", "aria-label": "Opciones" });\n    for (const b of botones) {\n      caja.append(h("button", { class: "opcion", type: "button", text: String(b.texto || ""), onclick: () => {\n        if (state.busy) return;\n        if (typeof b.elegir === "number") { caja.remove(); enviar("", b.texto, false, { elegir: b.elegir }); }\n        else if (b.mensaje) { caja.remove(); enviar(b.mensaje, b.texto); }\n        else if (b.prefijo) { caja.remove(); $("texto").value = b.prefijo; $("texto").focus(); }\n      } }));\n    }\n    return agregar(caja);\n  }\n  function escribiendo() {\n    return agregar(h("div", { class: "msg bot glass typing", "aria-label": "Escribiendo" }, h("span"), h("span"), h("span")));\n  }\n\n  function burbujaMovimiento(data) {\n    const m = data.movimiento, ingreso = m.tipo !== "gasto";\n    const notas = String(data.respuesta || "").split("\\n").filter((l) => l.startsWith("⚠️") || l.startsWith("🚨"));\n    const [d, mo, y] = [m.fecha.slice(8, 10), m.fecha.slice(5, 7), m.fecha.slice(0, 4)];\n    const b = h("div", { class: "msg bot glass mov mov-wrap" },\n      h("span", { class: "emoji", "aria-hidden": "true", text: ingreso ? "💰" : icono(m.categoria) }),\n      h("div", { class: "cuerpo" },\n        h("div", { class: "cat", text: m.categoria }),\n        m.ruta && m.ruta !== m.categoria ? h("div", { class: "meta", text: m.ruta }) : null,\n        m.descripcion ? h("div", { class: "desc", text: m.descripcion }) : null,\n        h("div", { class: "meta", text: `Bs. ${fmt(m.monto_bs)} · tasa ${fmt(m.tasa)} · ${d}/${mo}/${y}` })),\n      h("div", { class: "der" },\n        h("div", { class: "usd" + (ingreso ? " pos" : ""), text: `${ingreso ? "+" : "-"}$${fmt(m.monto_usd)}` }),\n        h("div", { class: "meta", text: m.hora })),\n      typeof m.saldo_usd === "number" ? h("div", { class: "meta", text: `Saldo estimado: $${fmt(m.saldo_usd)}` }) : null,\n      notas.map((n) => h("div", { class: "nota", text: n })));\n    return agregar(b);\n  }\n\n  /* ---------- resumen (saldo, ahorro, presupuestos) ---------- */\n  function renderResumen(r) {\n    const box = $("resumen");\n    if (!r.onboarding_completo) { box.hidden = true; return; }\n    state.onboarded = true;\n    const R = 31, C = 2 * Math.PI * R;\n    const frac = r.ingresos_mes_usd > 0 ? Math.max(0, Math.min(1, r.ahorro_mes_usd / r.ingresos_mes_usd)) : 0;\n    const prog = svg("circle", { class: "prog", cx: 37, cy: 37, r: R, "stroke-dasharray": C.toFixed(2), "stroke-dashoffset": (C * (1 - frac)).toFixed(2) });\n    const ring = svg("svg", { class: "ring", viewBox: "0 0 74 74", role: "img", "aria-label": `Ahorro del mes: ${Math.round(frac * 100)}% de los ingresos` });\n    ring.append(svg("circle", { class: "track", cx: 37, cy: 37, r: R }), prog);\n    const nodos = [h("div", { class: "glass saldo" }, ring,\n      h("div", null, h("div", { class: "label", text: "Saldo Disponible" }), h("div", { class: "monto", text: "$" + fmt(r.saldo_usd) }),\n        h("div", { class: "sub", text: "Ahorro del mes: $" + fmt(r.ahorro_mes_usd) })))];\n    for (const p of r.presupuestos || []) {\n      const pct = p.limite_usd > 0 ? p.gastado_usd / p.limite_usd : 0;\n      const bar = h("div", { class: "bar" + (pct >= 1 ? " over" : pct >= 0.8 ? " warn" : "") }, h("i"));\n      bar.firstChild.style.width = Math.min(100, pct * 100).toFixed(1) + "%";\n      nodos.push(h("div", { class: "glass presu" },\n        h("div", { class: "fila" }, h("span", { class: "nombre", text: `${icono(p.categoria)} Presupuesto ${p.categoria}` }),\n          h("span", { text: `-$${fmt(p.gastado_usd)} / -$${fmt(p.limite_usd)}` })), bar));\n    }\n    box.replaceChildren(...nodos);\n    box.hidden = false;\n  }\n  async function refrescarResumen() {\n    try { const r = await apiJson("/api/resumen"); renderResumen(r); marcarAlertas(r.alertas || 0); } catch (_) { /* el resumen es secundario */ }\n  }\n\n  /* ---------- reporte (modelo 3: píldoras desplegables) ---------- */\n  const COLORES = ["#34D399", "#38BDF8", "#A78BFA", "#FBBF24", "#F472B6", "#94A3B8"];\n  function donut(cats, total) {\n    const orden = Object.entries(cats).map(([n, v]) => [n, v.total_bs]);\n    const top = orden.slice(0, 5), resto = orden.slice(5).reduce((a, [, v]) => a + v, 0);\n    if (resto > 0) top.push(["Otros", resto]);\n    const s = svg("svg", { class: "donut", viewBox: "0 0 36 36", role: "img", "aria-label": "Distribución del gasto por categoría" });\n    const ley = h("div", { class: "leyenda" });\n    let acum = 0;\n    top.forEach(([n, v], i) => {\n      const p = (v / total) * 100, color = COLORES[i % COLORES.length];\n      s.append(svg("circle", { cx: 18, cy: 18, r: 15.9155, stroke: color, "stroke-dasharray": `${Math.max(p - 0.6, 0.1).toFixed(2)} ${(100 - Math.max(p - 0.6, 0.1)).toFixed(2)}`, "stroke-dashoffset": (-acum).toFixed(2) }));\n      acum += p;\n      const sw = h("i"); sw.style.background = color;\n      ley.append(h("div", null, sw, `${n} ${p.toFixed(1)}%`));\n    });\n    return h("div", { class: "donut-row" }, s, ley);\n  }\n  function burbujaReporte(rep) {\n    const anual = /^Resumen Anual/.test(rep.titulo);\n    const card = h("div", { class: "glass rep-card" },\n      h("div", { class: "rt", text: rep.titulo }),\n      h("div", { class: "tot", text: "Total Bs. " + fmt(rep.total_bs) }),\n      h("div", { class: "tot", text: "Total USD " + fmt(rep.total_usd) }),\n      rep.tasa_promedio ? h("div", { class: "prom", text: `Tasa promedio: ${fmt(rep.tasa_promedio)} Bs/USD` }) : null,\n      rep.total_bs > 0 ? donut(rep.categorias, rep.total_bs) : null);\n    const wrap = h("div", { class: "reporte" }, card, h("button", { class: "btn-s", type: "button", text: "📊 Ver detalle por local y categorías", onclick: () => panelInforme() }));\n    const pills = [];\n    Object.entries(rep.categorias).forEach(([cat, v], i) => {\n      const id = "pan-" + Date.now() + "-" + i;\n      const head = h("button", { class: "pill-head", type: "button", "aria-expanded": "false", "aria-controls": id },\n        h("span", { "aria-hidden": "true", text: icono(cat) }), h("span", { class: "t", text: `${cat} | -${fmt(v.total_bs)} Bs.` }),\n        h("span", { class: "chev", "aria-hidden": "true", text: "⌄" }));\n      const items = h("div", { class: "items" }, v.items.map((it) =>\n        h("div", { class: "item" }, h("span", { class: "f", text: anual ? it.fecha : it.fecha.slice(0, 5) }),\n          h("span", { class: "d", text: it.descripcion || cat }), h("span", { class: "bs", text: `-${fmt(it.monto_bs)} Bs.` }),\n          h("span", { class: "u", text: `($${fmt(it.monto_usd)})` }))));\n      const pill = h("div", { class: "pill" }, head, h("div", { class: "panel", id }, h("div", { class: "panel-inner" }, items)));\n      head.addEventListener("click", () => {\n        const abrir = !pill.classList.contains("open");\n        pills.forEach((p) => { p.classList.remove("open"); p.firstChild.setAttribute("aria-expanded", "false"); });\n        if (abrir) { pill.classList.add("open"); head.setAttribute("aria-expanded", "true"); }\n      });\n      pills.push(pill); wrap.append(pill);\n    });\n    return agregar(wrap, { top: true });\n  }\n\n  /* ---------- descarga CSV ---------- */\n  async function descargarCSV() {\n    try {\n      const res = await api("/api/exportar");\n      if (!res.ok) {\n        if (res.status === 401) bloquear("Tu sesión expiró. Ingresa el PIN de nuevo.");\n        let d = null; try { d = await res.json(); } catch (_) {}\n        throw new ApiError("http", (d && d.error) || `No se pudo generar el archivo (${res.status}).`, res.status);\n      }\n      const blob = await res.blob();\n      const m = /filename="?([^";]+)"?/.exec(res.headers.get("Content-Disposition") || "");\n      const nombre = m ? m[1] : "gastos.csv";\n      const url = URL.createObjectURL(blob);\n      const a = h("a", { href: url, download: nombre });\n      document.body.append(a); a.click(); a.remove();\n      setTimeout(() => URL.revokeObjectURL(url), 5000);\n      burbujaBot(`✅ Archivo listo: **${nombre}** (ábrelo con Excel o Google Sheets).`);\n    } catch (e) { burbujaError(e instanceof ApiError ? e : new ApiError("network", "No pude descargar el archivo."), descargarCSV); }\n  }\n\n  /* ---------- envío ---------- */\n  function ocupado(v) { state.busy = v; $("enviar").disabled = v; $("btn-reporte").disabled = v; }\n  async function enviar(texto, visible, reintento = false, cuerpo = null) {\n    texto = String(texto).trim();\n    if ((!texto && !cuerpo) || state.busy) return;\n    if (!reintento) burbujaUsuario(visible || texto);\n    ocupado(true);\n    const t = escribiendo();\n    try {\n      const data = await apiJson("/api/chat", { method: "POST", body: cuerpo || { mensaje: texto } });\n      t.remove();\n      if (data.tipo === "reporte" && data.reporte) burbujaReporte(data.reporte);\n      else if (data.tipo === "descarga") { burbujaBot(data.respuesta || "Descargando…"); await descargarCSV(); }\n      else if (data.movimiento) burbujaMovimiento(data);\n      else burbujaBot(data.respuesta || "…");\n      botonesBot(data.botones);\n      if (data.tipo !== "reporte") refrescarResumen();\n    } catch (e) {\n      t.remove();\n      if (!$("app").hidden) burbujaError(e, () => enviar(texto, visible, true, cuerpo));\n    } finally { ocupado(false); }\n  }\n\n  /* ---------- PIN ---------- */\n  function bloquear(msg) {\n    cerrarTodos();\n    $("app").hidden = true; $("lock").hidden = false; $("pin").value = "";\n    const e = $("pin-error"); e.textContent = msg || ""; e.hidden = !msg;\n  }\n  async function entrar() {\n    const pin = $("pin").value.trim(), err = $("pin-error");\n    err.hidden = true;\n    if (!$("nombre").value.trim()) { err.textContent = "Escribe tu nombre."; err.hidden = false; return; }\n    if (!/^\\d{4,6}$/.test(pin)) { err.textContent = "Ingresa tu PIN de 4 a 6 dígitos."; err.hidden = false; return; }\n    $("pin-btn").disabled = true;\n    try {\n      const nombre = $("nombre").value.trim();\n      const d = await apiJson("/api/auth", { method: "POST", body: { nombre, pin } });\n      $("pin").value = ""; $("lock").hidden = true; $("app").hidden = false;\n      chat().querySelectorAll(".msg, .reporte").forEach((n) => n.remove());\n      state.nombre = d.nombre || nombre;\n      burbujaBot(d.mensaje || "¡Hola!");\n      botonesBot(d.botones);\n      cargarAtajos();\n      if (d.onboarding_completo) refrescarResumen(); else $("resumen").hidden = true;\n      ajustarTeclado();\n    } catch (e) { err.textContent = e.message; err.hidden = false; $("pin").value = ""; }\n    finally { $("pin-btn").disabled = false; }\n  }\n\n  /* ---------- teclado virtual iOS ---------- */\n  function ajustarTeclado() {\n    const vv = window.visualViewport, root = document.documentElement.style;\n    root.setProperty("--app-h", (vv ? vv.height : window.innerHeight) + "px");\n    root.setProperty("--app-top", (vv ? vv.offsetTop : 0) + "px");\n    bajar();\n  }\n\n  /* ================= Paneles: Configuración, Historial, Facturas, Alertas e Informe ================= */\n  const CATALOGO = {\n    Dinero: "💰 💵 💳 🏦 🧾 📈 📉 🪙 💸 🧮",\n    Hogar: "🏠 🛋️ 🛏️ 🚿 🧹 🧺 🔧 🪛 🔑 🏡 💡 🔌",\n    Servicios: "💡 💧 🔥 📶 📱 📡 🌐 ☎️ 📺 🗑️",\n    Comida: "🍔 🍕 🌮 🥗 🍎 🥖 🍽️ ☕ 🍺 🍷 🛒 🥩 🐟 🍰",\n    Transporte: "⛽ 🚗 🚕 🚌 🚲 🏍️ ✈️ 🚆 🚢 🅿️ 🛞",\n    Salud: "💊 🩺 🏥 🦷 👓 🧴 🩹 🏋️ 🧘",\n    Negocio: "🏪 🏬 🏭 📦 🚚 🛍️ 🧰 🧵 🥫 🏷️ 📋 🖨️ 💼 👷",\n    Personal: "👤 👕 👟 💇 🎁 🎓 📚 🐶 🐱 👶 💍 ✂️",\n    Ocio: "🎉 🎬 🎮 🎵 ⚽ 🏖️ 🎟️ 📷 🎨 🧳 ⛪",\n    Otros: "📁 ⭐ ❤️ 🔔 ⚠️ 🛡️ 📞 ✉️ 🧩 🌱 ☀️ 🔒",\n  };\n  const ESTADO_TXT = { pendiente: "Pendiente", abonada: "Abonada", pagada: "Pagada" };\n  const moneda = (v, m) => (m === "usd" ? "$" + fmt(v) : "Bs. " + fmt(v));\n  const fechaCorta = (iso) => (iso ? `${iso.slice(8, 10)}/${iso.slice(5, 7)}/${iso.slice(0, 4)}` : "—");\n  const hoyIso = () => { const d = new Date(), p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };\n  const paneles = [];\n\n  function aplanar(nodos, nivel = 0, ruta = [], out = [], padre = null) {\n    for (const n of nodos || []) {\n      const r = [...ruta, n.nombre];\n      out.push({ ...n, nivel, padre_id: padre, ruta: r.join(" › ") });\n      aplanar(n.hijos, nivel + 1, r, out, n.id);\n    }\n    return out;\n  }\n  async function cargarArbol() { const d = await apiJson("/api/categorias"); state.arbol = Array.isArray(d.arbol) ? d.arbol : []; return aplanar(state.arbol); }\n  const opcionesCat = (lista, excluir) => lista.filter((c) => !excluir || !excluir.has(c.id)).map((c) => ({ v: c.id, t: `${c.icono} ${c.ruta}` }));\n  function subIds(lista, id) { // el nodo y sus descendientes (para no ofrecer mover una categoría dentro de sí misma)\n    const out = new Set([id]);\n    const hijosDe = (nodos) => { for (const n of nodos) { if (out.has(n.id)) todos(n); else hijosDe(n.hijos || []); } };\n    const todos = (n) => { for (const x of n.hijos || []) { out.add(x.id); todos(x); } };\n    hijosDe(state.arbol || []); return out;\n  }\n\n  /* --- panel a pantalla completa (con pila) --- */\n  function abrirPanel(titulo) {\n    const cerrar = h("button", { class: "p-x", type: "button", "aria-label": "Cerrar", text: "←" });\n    const t = h("h2", { class: "p-t", text: titulo });\n    const aviso = h("div", { class: "p-aviso", role: "alert", hidden: true });\n    const body = h("div", { class: "p-body" });\n    const el = h("div", { class: "p-full", role: "dialog", "aria-modal": "true", "aria-label": titulo }, h("div", { class: "p-head glass" }, cerrar, t), aviso, body);\n    const p = { el, body, titulo: t,\n      aviso(msg, ok) { aviso.textContent = msg || ""; aviso.hidden = !msg; aviso.className = "p-aviso" + (ok ? " ok" : ""); },\n      cerrar() { el.remove(); const i = paneles.indexOf(p); if (i >= 0) paneles.splice(i, 1); if (p.alCerrar) p.alCerrar(); },\n      async run(fn) { try { p.aviso(""); return await fn(); } catch (e) { if (e instanceof ApiError) p.aviso(e.message); else { p.aviso("Ocurrió un error inesperado."); } return null; } } };\n    cerrar.addEventListener("click", () => p.cerrar());\n    paneles.push(p); $("app").append(el); cerrar.focus();\n    return p;\n  }\n  const cerrarTodos = () => { while (paneles.length) paneles[paneles.length - 1].cerrar(); };\n  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const m = document.querySelector(".modal"); if (m) m.remove(); else if (paneles.length) paneles[paneles.length - 1].cerrar(); } });\n\n  /* --- diálogo con formulario --- */\n  function dialogo({ titulo, texto, campos = [], ok = "Guardar", peligro = false, onOk, extra = [] }) {\n    const err = h("div", { class: "p-aviso", role: "alert", hidden: true });\n    const vals = {}, refs = {};\n    const form = h("form", { class: "modal-card glass", autocomplete: "off" }, h("h3", { text: titulo }), texto ? h("p", { class: "muted", text: texto }) : null);\n    for (const c of campos) {\n      vals[c.k] = c.valor === undefined ? (c.tipo === "check" ? false : "") : c.valor;\n      let ctl;\n      if (c.tipo === "select") {\n        ctl = h("select", { id: "f-" + c.k }, (c.opciones || []).map((o) => h("option", { value: String(o.v), text: o.t })));\n        ctl.value = String(vals[c.k]);\n        ctl.addEventListener("change", () => { const o = c.opciones.find((x) => String(x.v) === ctl.value); vals[c.k] = o ? o.v : ctl.value; if (c.onChange) c.onChange(vals, refs); });\n      } else if (c.tipo === "check") {\n        ctl = h("input", { type: "checkbox", id: "f-" + c.k }); ctl.checked = !!vals[c.k];\n        ctl.addEventListener("change", () => { vals[c.k] = ctl.checked; });\n      } else if (c.tipo === "icono") {\n        const actual = h("button", { class: "icono-actual", type: "button", text: vals[c.k] || "📁", "aria-label": "Elegir ícono" });\n        const grid = h("div", { class: "icono-grid", hidden: true });\n        for (const [g, lista] of Object.entries(CATALOGO)) {\n          grid.append(h("div", { class: "icono-g", text: g }));\n          const fila = h("div", { class: "icono-fila" });\n          for (const ic of lista.split(" ")) fila.append(h("button", { class: "icono-op", type: "button", text: ic, "aria-label": ic, onclick: () => { vals[c.k] = ic; actual.textContent = ic; grid.hidden = true; } }));\n          grid.append(fila);\n        }\n        actual.addEventListener("click", () => { grid.hidden = !grid.hidden; });\n        ctl = h("div", null, actual, grid);\n      } else {\n        ctl = h(c.tipo === "area" ? "textarea" : "input", { id: "f-" + c.k, type: c.tipo === "area" ? null : (c.tipo || "text"), maxlength: c.max, placeholder: c.ph, inputmode: c.tipo === "number" ? "decimal" : null });\n        if (c.tipo === "number") ctl.type = "text";\n        ctl.value = vals[c.k] ?? "";\n        ctl.addEventListener("input", () => { vals[c.k] = ctl.value; });\n      }\n      refs[c.k] = ctl;\n      form.append(h("label", { class: "campo" + (c.tipo === "check" ? " check" : ""), for: "f-" + c.k }, h("span", { text: c.label }), ctl, c.ayuda ? h("small", { class: "muted", text: c.ayuda }) : null));\n    }\n    const cancelar = h("button", { class: "btn-s", type: "button", text: "Cancelar" });\n    const guardar = h("button", { class: "btn-s prim" + (peligro ? " peligro" : ""), type: "submit", text: ok });\n    form.append(err, h("div", { class: "modal-btns" }, cancelar, ...extra, guardar));\n    const m = h("div", { class: "modal" }, form);\n    cancelar.addEventListener("click", () => m.remove());\n    m.addEventListener("click", (e) => { if (e.target === m) m.remove(); });\n    form.addEventListener("submit", async (e) => {\n      e.preventDefault(); guardar.disabled = true; err.hidden = true;\n      try { await onOk(vals); m.remove(); }\n      catch (x) { err.textContent = x instanceof ApiError ? x.message : (x && x.message) || "No se pudo guardar."; err.hidden = false; guardar.disabled = false; }\n    });\n    $("app").append(m);\n    const primero = form.querySelector("input:not([type=checkbox]), select, textarea"); if (primero) primero.focus();\n    return { cerrar: () => m.remove(), refs, vals };\n  }\n  function confirmar(titulo, texto, ok, onOk) { return dialogo({ titulo, texto, ok, peligro: true, onOk }); }\n  function menuAcciones(titulo, acciones) {\n    const caja = h("div", { class: "modal-card glass" }, h("h3", { text: titulo }));\n    const m = h("div", { class: "modal" }, caja);\n    for (const a of acciones) caja.append(h("button", { class: "btn-s lista" + (a.peligro ? " peligro" : ""), type: "button", text: a.t, onclick: () => { m.remove(); a.f(); } }));\n    caja.append(h("button", { class: "btn-s", type: "button", text: "Cancelar", onclick: () => m.remove() }));\n    m.addEventListener("click", (e) => { if (e.target === m) m.remove(); });\n    $("app").append(m); return m;\n  }\n  const seccion = (titulo, ...kids) => h("section", { class: "sec glass" }, h("h3", { text: titulo }), ...kids);\n  const filaDato = (a, b) => h("div", { class: "kv" }, h("span", { class: "muted", text: a }), h("span", { text: b }));\n  const vacio = (t) => h("p", { class: "muted vacio", text: t });\n  function badge(txt, cls) { return h("span", { class: "tag " + (cls || ""), text: txt }); }\n\n  /* --- insignia (!) de alertas y accesos rápidos --- */\n  function marcarAlertas(n) { const b = $("alerta-badge"); if (b) b.hidden = !(n > 0); const btn = $("btn-alertas"); if (btn) btn.setAttribute("aria-label", n > 0 ? `Alertas: ${n} pendientes` : "Alertas"); }\n  async function cargarAtajos() {\n    try {\n      const d = await apiJson("/api/atajos");\n      if (!Array.isArray(d.atajos)) return;\n      $("chips").replaceChildren(...d.atajos.map((a) => h("button", { class: "chip", type: "button", "data-prefijo": a.prefijo, text: `${a.icono} ${a.nombre}` })));\n    } catch (_) { /* quedan los accesos por defecto */ }\n  }\n\n  /* ================= Configuración ================= */\n  async function panelConfig() {\n    const p = abrirPanel("Configuración");\n    const b = p.body;\n    const render = async () => {\n      const [aj, pref, lista, at] = await Promise.all([apiJson("/api/ajustes"), apiJson("/api/preferencias"), cargarArbol(), apiJson("/api/atajos")]);\n      b.replaceChildren(secPerfil(p), secTasaSaldo(p, aj), secFacturas(p, pref), secCategorias(p, lista, render), secAtajos(p, lista, at), secPapelera(p),\n        h("button", { class: "btn-s peligro salir", type: "button", text: "Cerrar sesión", onclick: async () => { try { await apiJson("/api/logout", { method: "POST" }); } catch (_) {} cerrarTodos(); bloquear(""); } }));\n    };\n    await p.run(render);\n  }\n  function secPerfil(p) {\n    const nombre = h("input", { type: "text", maxlength: 40, value: state.nombre || "", "aria-label": "Nombre" });\n    return seccion("Perfil",\n      h("div", { class: "fila-inp" }, nombre, h("button", { class: "btn-s prim", type: "button", text: "Guardar nombre", onclick: () => p.run(async () => {\n        const d = await apiJson("/api/nombre", { method: "PUT", body: { nombre: nombre.value } }); state.nombre = d.nombre; p.aviso("Nombre actualizado ✅", true); }) })),\n      h("button", { class: "btn-s", type: "button", text: "Cambiar PIN", onclick: () => dialogo({ titulo: "Cambiar PIN", campos: [\n        { k: "actual", label: "PIN actual", tipo: "password", max: 6 }, { k: "nuevo", label: "PIN nuevo (4 a 6 dígitos)", tipo: "password", max: 6 }, { k: "rep", label: "Repite el PIN nuevo", tipo: "password", max: 6 }],\n        onOk: async (v) => { if (v.nuevo !== v.rep) throw new Error("Los PIN nuevos no coinciden."); if (!/^\\d{4,6}$/.test(v.nuevo)) throw new Error("El PIN nuevo debe tener de 4 a 6 dígitos.");\n          await apiJson("/api/pin", { method: "PUT", body: { actual: v.actual, nuevo: v.nuevo } }); p.aviso("PIN cambiado ✅", true); } }) }));\n  }\n  function secTasaSaldo(p, aj) {\n    const tasa = h("input", { type: "text", inputmode: "decimal", placeholder: "Ej: 915", "aria-label": "Tasa de hoy" });\n    const saldo = h("input", { type: "text", inputmode: "decimal", value: String(aj.saldo_inicial_usd), "aria-label": "Saldo inicial en dólares" });\n    return seccion("Tasa y saldo",\n      h("label", { class: "campo" }, h("span", { text: "Tasa de hoy (Bs por USD)" }), h("div", { class: "fila-inp" }, tasa, h("button", { class: "btn-s prim", type: "button", text: "Guardar tasa", onclick: () => p.run(async () => {\n        const d = await apiJson("/api/chat", { method: "POST", body: { mensaje: "tasa " + tasa.value.trim() } }); p.aviso(String(d.respuesta || "Listo").replace(/\\*\\*/g, ""), true); refrescarResumen(); }) }))),\n      h("label", { class: "campo" }, h("span", { text: "Saldo inicial (USD) — editable cuando quieras" }), h("div", { class: "fila-inp" }, saldo, h("button", { class: "btn-s prim", type: "button", text: "Guardar saldo", onclick: () => p.run(async () => {\n        const d = await apiJson("/api/ajustes", { method: "PUT", body: { saldo_inicial_usd: saldo.value } }); saldo.value = String(d.saldo_inicial_usd); p.aviso("Saldo inicial actualizado ✅", true); refrescarResumen(); }) }))));\n  }\n  function secFacturas(p, pref) {\n    const solo = h("input", { type: "checkbox", id: "pref-solo" }); solo.checked = !!pref.solo_bs_defecto;\n    const dias = h("input", { type: "text", inputmode: "numeric", value: String(pref.dias_aviso), maxlength: 2, "aria-label": "Días de aviso" });\n    return seccion("Facturas",\n      h("label", { class: "campo check", for: "pref-solo" }, h("span", { text: "«Solo Bs» activado por defecto en facturas en bolívares" }), solo, h("small", { class: "muted", text: "Apagado: las facturas se convierten a dólares. Se puede activar factura por factura." })),\n      h("label", { class: "campo" }, h("span", { text: "Avisar cuántos días antes del vencimiento" }), dias),\n      h("button", { class: "btn-s prim", type: "button", text: "Guardar preferencias", onclick: () => p.run(async () => {\n        await apiJson("/api/preferencias", { method: "PUT", body: { solo_bs_defecto: solo.checked, dias_aviso: Number(dias.value) } }); p.aviso("Preferencias guardadas ✅", true); refrescarResumen(); }) }));\n  }\n  function secPapelera(p) {\n    return seccion("Papelera", h("p", { class: "muted", text: "Lo que borras se puede recuperar desde aquí." }),\n      h("div", { class: "fila-btns" }, h("button", { class: "btn-s", type: "button", text: "Movimientos borrados", onclick: () => panelHistorial(true) }),\n        h("button", { class: "btn-s", type: "button", text: "Facturas borradas", onclick: () => panelFacturas("papelera") })));\n  }\n  function secCategorias(p, lista, recargar) {\n    const caja = seccion("Categorías", h("p", { class: "muted", text: "PERSONAL y NEGOCIOS son fijas. Los locales cuelgan de NEGOCIOS; lo que está en NEGOCIOS fuera de un local es de todos." }),\n      h("button", { class: "btn-s prim", type: "button", text: "＋ Nueva categoría", onclick: () => formCategoria(p, lista, null, recargar) }));\n    const ul = h("div", { class: "arbol" });\n    for (const c of lista) {\n      const fila = h("div", { class: "nodo", style: null }, h("span", { class: "ic", text: c.icono }), h("span", { class: "nm", text: c.nombre }),\n        c.es_local ? badge("local", "ok") : null, c.fija ? badge("base") : null,\n        h("button", { class: "mas", type: "button", "aria-label": `Acciones de ${c.nombre}`, text: "⋯", onclick: () => menuCategoria(p, lista, c, recargar) }));\n      fila.style.paddingLeft = (8 + c.nivel * 18) + "px"; ul.append(fila);\n    }\n    caja.append(ul); return caja;\n  }\n  function menuCategoria(p, lista, c, recargar) {\n    const acc = [{ t: "＋ Subcategoría aquí", f: () => formCategoria(p, lista, { padre: c }, recargar) }];\n    if (!c.fija) acc.push({ t: "✏️ Editar (nombre, ícono, mover)", f: () => formCategoria(p, lista, { editar: c }, recargar) },\n      { t: "🔀 Fusionar con otra categoría", f: () => formFusion(p, lista, c, recargar) },\n      { t: "🗑️ Eliminar", peligro: true, f: () => formEliminar(p, lista, c, recargar) });\n    else acc.push({ t: "🎨 Cambiar ícono", f: () => formCategoria(p, lista, { editar: c, soloIcono: true }, recargar) });\n    menuAcciones(`${c.icono} ${c.nombre}`, acc);\n  }\n  function formCategoria(p, lista, modo, recargar) {\n    const editar = modo && modo.editar, padre = modo && modo.padre;\n    const excl = editar ? subIds(lista, editar.id) : null;\n    const padres = opcionesCat(lista, excl);\n    const padreActual = editar ? editar.padre_id : (padre ? padre.id : (lista[0] || {}).id);\n    const campos = [];\n    if (!(modo && modo.soloIcono)) campos.push({ k: "nombre", label: "Nombre", valor: editar ? editar.nombre : "", max: 60 });\n    campos.push({ k: "icono", label: "Ícono", tipo: "icono", valor: editar ? editar.icono : "📁" });\n    if (!(modo && modo.soloIcono)) campos.push({ k: "padre", label: editar ? "Mover a" : "Dentro de", tipo: "select", opciones: padres, valor: padreActual },\n      { k: "local", label: "Es un local (solo directo bajo NEGOCIOS)", tipo: "check", valor: editar ? !!editar.es_local : false });\n    dialogo({ titulo: editar ? "Editar categoría" : "Nueva categoría", campos, onOk: async (v) => {\n      if (editar) {\n        const cuerpo = { icono: v.icono };\n        if (!(modo.soloIcono)) { cuerpo.nombre = v.nombre; if (v.padre !== padreActual) cuerpo.padre_id = v.padre; if (!!v.local !== !!editar.es_local) cuerpo.es_local = !!v.local; }\n        await apiJson("/api/categorias/" + editar.id, { method: "PUT", body: cuerpo });\n      } else await apiJson("/api/categorias", { method: "POST", body: { nombre: v.nombre, padre_id: v.padre, icono: v.icono, es_local: !!v.local } });\n      await recargar(); cargarAtajos(); } });\n  }\n  function formFusion(p, lista, c, recargar) {\n    const excl = subIds(lista, c.id);\n    dialogo({ titulo: `Fusionar «${c.nombre}»`, texto: "Sus movimientos, límites y subcategorías pasan a la categoría elegida, y esta se elimina.", ok: "Fusionar", peligro: true,\n      campos: [{ k: "dest", label: "Fusionar con", tipo: "select", opciones: opcionesCat(lista, excl) }],\n      onOk: async (v) => { if (v.dest === "" || v.dest == null) throw new Error("Elige la categoría destino."); await apiJson(`/api/categorias/${c.id}/fusionar`, { method: "POST", body: { destino_id: v.dest } }); await recargar(); cargarAtajos(); } });\n  }\n  function formEliminar(p, lista, c, recargar) {\n    const excl = subIds(lista, c.id);\n    dialogo({ titulo: `Eliminar «${c.nombre}»`, texto: "Si tiene movimientos, facturas o subcategorías, elige a dónde pasarlos. Si está vacía se elimina directamente.", ok: "Eliminar", peligro: true,\n      campos: [{ k: "dest", label: "Pasar todo a (opcional)", tipo: "select", opciones: [{ v: "", t: "— Ninguna (solo si está vacía) —" }, ...opcionesCat(lista, excl)], valor: "" }],\n      onOk: async (v) => { await apiJson(`/api/categorias/${c.id}` + (v.dest !== "" ? `?destino=${encodeURIComponent(v.dest)}` : ""), { method: "DELETE" }); await recargar(); cargarAtajos(); } });\n  }\n  function secAtajos(p, lista, at) {\n    let sel = (at.atajos || []).map((a) => a.categoria_id); const max = at.maximo || 8;\n    const caja = seccion("Accesos rápidos", h("p", { class: "muted", text: `Botones de arriba del teclado (hasta ${max}). Al tocarlos escriben la categoría por ti.` }));\n    const ul = h("div", { class: "atajos" });\n    const pintar = () => {\n      ul.replaceChildren(...sel.map((id, i) => { const c = lista.find((x) => x.id === id); if (!c) return null;\n        return h("div", { class: "atajo" }, h("span", { text: `${c.icono} ${c.ruta}` }),\n          h("button", { class: "mas", type: "button", "aria-label": "Subir", text: "▲", disabled: i === 0 ? "" : null, onclick: () => { [sel[i - 1], sel[i]] = [sel[i], sel[i - 1]]; pintar(); } }),\n          h("button", { class: "mas", type: "button", "aria-label": "Bajar", text: "▼", disabled: i === sel.length - 1 ? "" : null, onclick: () => { [sel[i + 1], sel[i]] = [sel[i], sel[i + 1]]; pintar(); } }),\n          h("button", { class: "mas", type: "button", "aria-label": "Quitar", text: "✕", onclick: () => { sel.splice(i, 1); pintar(); } })); }));\n      if (!sel.length) ul.append(vacio("Sin accesos rápidos."));\n    };\n    const elegir = h("select", { "aria-label": "Añadir categoría" }, opcionesCat(lista).map((o) => h("option", { value: String(o.v), text: o.t })));\n    caja.append(ul, h("div", { class: "fila-inp" }, elegir, h("button", { class: "btn-s", type: "button", text: "＋ Añadir", onclick: () => { const id = Number(elegir.value); if (!sel.includes(id) && sel.length < max) { sel.push(id); pintar(); } else p.aviso(sel.includes(id) ? "Ya está en la lista." : `Máximo ${max} accesos.`); } })),\n      h("button", { class: "btn-s prim", type: "button", text: "Guardar accesos rápidos", onclick: () => p.run(async () => { await apiJson("/api/atajos", { method: "PUT", body: { categorias: sel } }); await cargarAtajos(); p.aviso("Accesos rápidos guardados ✅", true); }) }));\n    pintar(); return caja;\n  }\n\n  /* ================= Historial ================= */\n  async function panelHistorial(papelera = false) {\n    const p = abrirPanel(papelera ? "Papelera de movimientos" : "Historial");\n    const f = { tipo: "todos", local: "", desde: "", hasta: "", q: "", papelera: papelera ? 1 : 0 }; let items = [], total = 0, suma = null;\n    const lista = h("div", { class: "lista" }), pie = h("div", { class: "pie" }), resu = h("div", { class: "resu" });\n    const cats = await p.run(async () => { const c = await cargarArbol(); return c; }) || [];\n    const locales = cats.filter((c) => c.es_local);\n    const selTipo = h("select", { "aria-label": "Tipo" }, [["todos", "Todo"], ["gasto", "Gastos"], ["deposito", "Depósitos"]].map(([v, t]) => h("option", { value: v, text: t })));\n    const selLocal = h("select", { "aria-label": "Local" }, [["", "Todas las categorías"], ["personal", "Personal"], ["compartido", "Compartido (negocios)"], ...locales.map((l) => [String(l.id), "🏬 " + l.nombre])].map(([v, t]) => h("option", { value: v, text: t })));\n    const desde = h("input", { type: "date", "aria-label": "Desde" }), hasta = h("input", { type: "date", "aria-label": "Hasta" }), q = h("input", { type: "text", placeholder: "Buscar…", maxlength: 60, "aria-label": "Buscar" });\n    const qs = (offset) => { const u = new URLSearchParams({ tipo: f.tipo, papelera: String(f.papelera), limit: "50", offset: String(offset) }); for (const k of ["local", "desde", "hasta", "q"]) if (f[k]) u.set(k, f[k]); return "/api/historial?" + u; };\n    const fila = (it) => h("button", { class: "item-h", type: "button", onclick: () => accionesMov(it) },\n      h("span", { class: "ic", text: it.tipo === "deposito" ? "💰" : (cats.find((c) => c.id === it.categoria_id) || {}).icono || "🧾" }),\n      h("span", { class: "cu" }, h("span", { class: "t1", text: it.descripcion || it.categoria }), h("span", { class: "t2 muted", text: `${it.ruta} · ${fechaCorta(it.fecha)}` }),\n        it.factura || it.solo_bs ? h("span", null, it.factura ? badge("Factura " + it.factura.numero) : null, it.solo_bs ? badge("solo Bs") : null) : null),\n      h("span", { class: "mo " + (it.tipo === "deposito" ? "pos" : "") }, h("span", { text: (it.tipo === "deposito" ? "+" : "-") + (it.solo_bs ? "Bs. " + fmt(it.monto_bs) : "$" + fmt(it.monto_usd)) }), it.solo_bs ? null : h("small", { class: "muted", text: "Bs. " + fmt(it.monto_bs) })));\n    const pintar = () => {\n      lista.replaceChildren(...(items.length ? items.map(fila) : [vacio(papelera ? "La papelera está vacía." : "No hay movimientos con esos filtros.")]));\n      resu.replaceChildren(...(suma ? [h("span", { text: `${total} movimientos` }), h("span", { text: `Gastos $${fmt(suma.gastos_usd)}` }), h("span", { text: `Depósitos $${fmt(suma.depositos_usd)}` })] : []));\n      pie.replaceChildren(items.length < total ? h("button", { class: "btn-s", type: "button", text: "Cargar más", onclick: () => cargar(true) }) : "",\n        papelera && items.length ? h("button", { class: "btn-s peligro", type: "button", text: "Vaciar papelera", onclick: () => confirmar("Vaciar la papelera", "Se borrará para siempre todo lo que está en la papelera (también facturas y pagos). No se puede deshacer.", "Vaciar", async () => { await apiJson("/api/papelera/vaciar", { method: "POST", body: {} }); await cargar(false); }) }) : "");\n    };\n    const cargar = (mas) => p.run(async () => { const d = await apiJson(qs(mas ? items.length : 0)); if (!Array.isArray(d.items)) throw new ApiError("badjson", "Respuesta inválida del servidor."); items = mas ? items.concat(d.items) : d.items; total = d.total; suma = d.suma; pintar(); });\n    const aplicar = () => { f.tipo = selTipo.value; f.local = selLocal.value; f.desde = desde.value; f.hasta = hasta.value; f.q = q.value.trim(); return cargar(false); };\n    const accionesMov = (it) => papelera\n      ? menuAcciones(`${it.categoria} · ${fechaCorta(it.fecha)}`, [{ t: "↩️ Restaurar", f: () => p.run(async () => { await apiJson(`/api/movimientos/${it.id}/restaurar`, { method: "POST", body: {} }); await cargar(false); refrescarResumen(); }) },\n        { t: "🗑️ Eliminar para siempre", peligro: true, f: () => confirmar("Eliminar para siempre", "No se podrá recuperar.", "Eliminar", async () => { await apiJson(`/api/movimientos/${it.id}/definitivo`, { method: "DELETE" }); await cargar(false); }) }])\n      : menuAcciones(`${it.categoria} · ${fechaCorta(it.fecha)}`, [{ t: "✏️ Editar categoría o descripción", f: () => dialogo({ titulo: "Editar movimiento", texto: "El monto, la fecha y la tasa no se editan: si hay un error, bórralo y vuelve a anotarlo.", campos: [\n          { k: "cat", label: "Categoría", tipo: "select", opciones: opcionesCat(cats), valor: it.categoria_id }, { k: "desc", label: "Descripción", valor: it.descripcion || "", max: 200 }],\n          onOk: async (v) => { await apiJson("/api/movimientos/" + it.id, { method: "PUT", body: { categoria_id: v.cat, descripcion: v.desc } }); await cargar(false); } }) },\n        { t: "🗑️ Borrar (va a la papelera)", peligro: true, f: () => confirmar("Borrar movimiento", it.factura ? "Era el pago de una factura: el faltante se recupera. Podrás restaurarlo desde la papelera." : "Podrás restaurarlo desde la papelera.", "Borrar", async () => { await apiJson("/api/movimientos/" + it.id, { method: "DELETE" }); await cargar(false); refrescarResumen(); }) }]);\n    p.body.append(h("div", { class: "filtros glass" }, selTipo, selLocal, desde, hasta, q, h("button", { class: "btn-s prim", type: "button", text: "Aplicar", onclick: aplicar }),\n      papelera ? null : h("button", { class: "btn-s", type: "button", text: "📊 Informe", onclick: () => panelInforme() })), resu, lista, pie);\n    q.addEventListener("keydown", (e) => { if (e.key === "Enter") aplicar(); });\n    await cargar(false);\n  }\n\n  /* ================= Facturas ================= */\n  async function panelFacturas(estado = "pendientes") {\n    const p = abrirPanel("Facturas"); let actual = estado;\n    const lista = h("div", { class: "lista" });\n    const tabs = h("div", { class: "tabs" });\n    const pintarTabs = () => tabs.replaceChildren(...[["pendientes", "Pendientes"], ["pagadas", "Pagadas"], ["papelera", "Papelera"]].map(([v, t]) =>\n      h("button", { class: "tab" + (actual === v ? " on" : ""), type: "button", text: t, onclick: () => { actual = v; cargar(); } })));\n    const cargar = () => p.run(async () => {\n      pintarTabs(); const d = await apiJson("/api/facturas?estado=" + actual);\n      if (!Array.isArray(d.facturas)) throw new ApiError("badjson", "Respuesta inválida del servidor.");\n      lista.replaceChildren(...(d.facturas.length ? d.facturas.map((f) => filaFactura(f, p, cargar, actual === "papelera")) : [vacio(actual === "papelera" ? "No hay facturas en la papelera." : actual === "pagadas" ? "Aún no hay facturas pagadas." : "No tienes facturas pendientes 🎉")]));\n    });\n    p.alCerrar = refrescarResumen;\n    p.body.append(h("div", { class: "fila-btns" }, h("button", { class: "btn-s prim", type: "button", text: "＋ Nueva factura", onclick: () => p.run(async () => { await cargarArbol(); formFactura(p, cargar); }) })), tabs, lista);\n    await cargar();\n  }\n  function textoAlerta(a) {\n    if (!a) return null;\n    if (a.tipo === "vencida") return badge(`Vencida hace ${a.dias} día${a.dias === 1 ? "" : "s"}`, "mal");\n    if (a.tipo === "por_vencer") return badge(a.dias === 0 ? "Vence hoy" : `Vence en ${a.dias} día${a.dias === 1 ? "" : "s"}`, "aviso");\n    return badge("Falta definir el crédito", "aviso");\n  }\n  function filaFactura(f, p, recargar, enPapelera) {\n    return h("button", { class: "item-h", type: "button", onclick: () => (enPapelera ? menuAcciones(`Factura ${f.numero}`, [\n      { t: "↩️ Restaurar", f: () => p.run(async () => { await apiJson(`/api/facturas/${f.id}/restaurar`, { method: "POST", body: {} }); await recargar(); }) },\n      { t: "🗑️ Eliminar para siempre", peligro: true, f: () => confirmar("Eliminar para siempre", "Se borran también sus pagos.", "Eliminar", async () => { await apiJson(`/api/facturas/${f.id}/definitivo`, { method: "DELETE" }); await recargar(); }) }]) : panelFactura(f.id, recargar)) },\n      h("span", { class: "ic", text: "🧾" }),\n      h("span", { class: "cu" }, h("span", { class: "t1", text: f.proveedor }), h("span", { class: "t2 muted", text: `#${f.numero} · vence ${fechaCorta(f.vence)}` }),\n        h("span", null, badge(ESTADO_TXT[f.estado] || f.estado, f.estado === "pagada" ? "ok" : ""), f.solo_bs ? badge("solo Bs") : null, textoAlerta(f.alerta))),\n      h("span", { class: "mo" }, h("span", { text: moneda(f.estado === "pagada" ? f.monto : f.faltante, f.moneda) }), h("small", { class: "muted", text: f.estado === "pagada" ? "pagada" : "falta" })));\n  }\n  function formFactura(p, recargar, f) {\n    const edit = !!f; const cats = aplanar(state.arbol || []);\n    const neg = cats.find((c) => c.nombre === "NEGOCIOS" && c.nivel === 0);\n    dialogo({ titulo: edit ? "Editar factura" : "Nueva factura", campos: [\n      { k: "numero", label: "Número", valor: edit ? f.numero : "", max: 40 }, { k: "proveedor", label: "Proveedor", valor: edit ? f.proveedor : "", max: 100 },\n      { k: "fecha", label: "Fecha de emisión", tipo: "date", valor: edit ? f.fecha_emision : hoyIso() },\n      { k: "monto", label: "Monto", tipo: "number", valor: edit ? String(f.monto) : "" },\n      ...(edit ? [] : [{ k: "moneda", label: "Moneda", tipo: "select", opciones: [{ v: "usd", t: "Dólares (USD)" }, { v: "bs", t: "Bolívares (Bs)" }], valor: "usd",\n        onChange: (v, r) => { const w = r.solo_bs.closest("label"); w.hidden = v.moneda !== "bs"; if (v.moneda !== "bs") { v.solo_bs = false; r.solo_bs.checked = false; } } },\n        { k: "solo_bs", label: "Solo Bs: no convertir a dólares (caso puntual)", tipo: "check", valor: false }]),\n      { k: "dias", label: "Días de crédito", tipo: "number", valor: edit && f.dias_credito != null ? String(f.dias_credito) : "", ayuda: "Si lo dejas vacío, la app te avisará (!) hasta que lo definas." },\n      ...(edit ? [] : [{ k: "cat", label: "Categoría del gasto", tipo: "select", opciones: opcionesCat(cats), valor: neg ? neg.id : undefined }]),\n      { k: "nota", label: "Nota (opcional)", valor: edit ? f.nota || "" : "", max: 200 }],\n      onOk: async (v) => {\n        const cuerpo = { numero: v.numero, proveedor: v.proveedor, fecha_emision: v.fecha, monto: v.monto, dias_credito: String(v.dias).trim() === "" ? null : Number(v.dias), nota: v.nota };\n        if (edit) await apiJson("/api/facturas/" + f.id, { method: "PUT", body: cuerpo });\n        else await apiJson("/api/facturas", { method: "POST", body: { ...cuerpo, moneda: v.moneda, solo_bs: v.moneda === "bs" && !!v.solo_bs, categoria_id: v.cat } });\n        await recargar(); refrescarResumen(); } });\n    const w = document.querySelector(".modal #f-solo_bs"); if (w && !edit) w.closest("label").hidden = true;\n  }\n  async function panelFactura(id, recargar) {\n    const p = abrirPanel("Factura"); p.alCerrar = () => { if (recargar) recargar(); refrescarResumen(); };\n    const render = () => p.run(async () => {\n      const d = await apiJson("/api/facturas/" + id); const f = d.factura; if (!f || typeof f !== "object") throw new ApiError("badjson", "Respuesta inválida del servidor.");\n      p.titulo.textContent = `Factura ${f.numero}`;\n      const abonos = h("div", { class: "lista" }, ...(f.abonos.length ? f.abonos.map((a) => h("div", { class: "abono" }, h("span", { text: `${fechaCorta(a.fecha)} · ${moneda(a.monto, f.moneda)}` }),\n        h("button", { class: "mas", type: "button", "aria-label": "Anular pago", text: "✕", onclick: () => confirmar("Anular pago", "El gasto de ese pago va a la papelera y el faltante se recupera.", "Anular", async () => { await apiJson(`/api/facturas/${f.id}/pagos/${a.id}`, { method: "DELETE" }); await render(); refrescarResumen(); }) }))) : [vacio("Sin pagos todavía.")]));\n      const acciones = h("div", { class: "fila-btns" });\n      if (f.estado !== "pagada") acciones.append(\n        h("button", { class: "btn-s prim", type: "button", text: "✅ Pagar todo", onclick: () => dialogo({ titulo: "Pagar todo", texto: `Se registra el pago de ${moneda(f.faltante, f.moneda)}.`, ok: "Pagar", campos: [{ k: "fecha", label: "Fecha del pago", tipo: "date", valor: hoyIso() }],\n          onOk: async (v) => { await apiJson(`/api/facturas/${f.id}/pagos`, { method: "POST", body: { fecha: v.fecha } }); await render(); refrescarResumen(); } }) }),\n        h("button", { class: "btn-s", type: "button", text: "➕ Abonar", onclick: () => dialogo({ titulo: "Registrar abono", texto: `Falta ${moneda(f.faltante, f.moneda)}.`, ok: "Abonar", campos: [\n          { k: "monto", label: "Monto del abono", tipo: "number", valor: "" }, { k: "fecha", label: "Fecha del pago", tipo: "date", valor: hoyIso() }],\n          onOk: async (v) => { await apiJson(`/api/facturas/${f.id}/pagos`, { method: "POST", body: { monto: v.monto, fecha: v.fecha } }); await render(); refrescarResumen(); } }) }));\n      acciones.append(h("button", { class: "btn-s", type: "button", text: "✏️ Editar", onclick: async () => { await p.run(cargarArbol); formFactura(p, render, f); } }),\n        h("button", { class: "btn-s peligro", type: "button", text: "🗑️ Borrar", onclick: () => confirmar("Borrar factura", "Va a la papelera con sus pagos; podrás restaurarla.", "Borrar", async () => { await apiJson("/api/facturas/" + f.id, { method: "DELETE" }); p.cerrar(); refrescarResumen(); }) }));\n      p.body.replaceChildren(seccion(f.proveedor, filaDato("Número", f.numero), filaDato("Emisión", fechaCorta(f.fecha_emision)),\n        filaDato("Crédito", f.dias_credito == null ? "Sin definir" : `${f.dias_credito} días`), filaDato("Vence", fechaCorta(f.vence)), filaDato("Monto", moneda(f.monto, f.moneda)),\n        filaDato("Pagado", moneda(f.pagado, f.moneda)), filaDato("Falta", moneda(f.faltante, f.moneda)), filaDato("Categoría", f.ruta),\n        f.solo_bs ? filaDato("Conversión", "Solo Bs (no se convierte a dólares)") : null, f.nota ? filaDato("Nota", f.nota) : null,\n        h("div", null, badge(ESTADO_TXT[f.estado] || f.estado, f.estado === "pagada" ? "ok" : ""), textoAlerta(f.alerta))), acciones, seccion("Pagos", abonos));\n    });\n    await render();\n  }\n\n  /* ================= Alertas (!) ================= */\n  async function panelAlertas() {\n    const p = abrirPanel("Alertas"); const lista = h("div", { class: "lista" });\n    const cargar = () => p.run(async () => {\n      const d = await apiJson("/api/alertas"); if (!Array.isArray(d.alertas)) throw new ApiError("badjson", "Respuesta inválida del servidor.");\n      marcarAlertas(d.alertas.length);\n      lista.replaceChildren(...(d.alertas.length ? d.alertas.map((a) => h("div", { class: "alerta " + (a.tipo === "vencida" ? "mal" : "aviso") },\n        h("div", { class: "cu" }, h("span", { class: "t1", text: `${a.proveedor} · #${a.numero}` }), h("span", { class: "t2 muted", text: `Falta ${moneda(a.faltante, a.moneda)}` + (a.vence ? ` · vence ${fechaCorta(a.vence)}` : "") }), textoAlerta({ tipo: a.tipo, dias: a.dias })),\n        h("div", { class: "fila-btns" }, h("button", { class: "btn-s", type: "button", text: "Ver", onclick: () => panelFactura(a.factura_id, cargar) }),\n          a.tipo === "sin_credito" ? h("button", { class: "btn-s prim", type: "button", text: "Definir crédito", onclick: () => dialogo({ titulo: "Días de crédito", campos: [{ k: "dias", label: `Factura #${a.numero}`, tipo: "number", ph: "Ej: 30" }],\n            onOk: async (v) => { if (!/^\\d{1,3}$/.test(String(v.dias).trim())) throw new Error("Escribe un número de días (0 a 365)."); await apiJson("/api/facturas/" + a.factura_id, { method: "PUT", body: { dias_credito: Number(v.dias) } }); await cargar(); } }) }) : null))) : [vacio("No tienes alertas ✅")]));\n    });\n    p.alCerrar = refrescarResumen; p.body.append(lista); await cargar();\n  }\n\n  /* ================= Informe detallado ================= */\n  async function panelInforme() {\n    const p = abrirPanel("Informe"); const hoy = new Date();\n    const cats = await p.run(cargarArbol) || []; const locales = cats.filter((c) => c.es_local);\n    const mes = h("select", { "aria-label": "Mes" }, [["0", "Año completo"], ...["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"].map((m, i) => [String(i + 1), m])].map(([v, t]) => h("option", { value: v, text: t })));\n    mes.value = String(hoy.getMonth() + 1);\n    const anio = h("input", { type: "text", inputmode: "numeric", maxlength: 4, value: String(hoy.getFullYear()), "aria-label": "Año" });\n    const loc = h("select", { "aria-label": "Filtrar" }, [["", "Todo"], ["personal", "Personal"], ["compartido", "Compartido (negocios)"], ...locales.map((l) => [String(l.id), "🏬 " + l.nombre])].map(([v, t]) => h("option", { value: v, text: t })));\n    const rep = h("input", { type: "checkbox", id: "inf-rep" }); const salida = h("div", { class: "informe" });\n    const nodo = (n, nivel) => { const hijos = n.hijos || []; const fila = h("div", { class: "inf-nodo" }, h("span", { class: "ic", text: n.icono }), h("span", { class: "nm", text: n.nombre }), n.es_local ? badge("local", "ok") : null, h("span", { class: "mo", text: `$${fmt(n.total_usd)} · Bs. ${fmt(n.total_bs)}` })); fila.style.paddingLeft = (6 + nivel * 16) + "px"; return [fila, ...hijos.flatMap((x) => nodo(x, nivel + 1))]; };\n    const cargar = () => p.run(async () => {\n      if (!/^\\d{4}$/.test(anio.value.trim())) throw new ApiError("http", "Escribe un año de 4 dígitos.");\n      const u = new URLSearchParams({ anio: anio.value.trim(), mes: mes.value }); if (loc.value) u.set("local", loc.value); if (rep.checked) u.set("repartir", "1");\n      const d = await apiJson("/api/reporte?" + u); const r = d.reporte; if (!r || !Array.isArray(r.arbol)) throw new ApiError("badjson", "Respuesta inválida del servidor.");\n      const kids = [seccion(r.titulo, filaDato("Total", `$${fmt(r.total_usd)} · Bs. ${fmt(r.total_bs)}`), r.tasa_promedio ? filaDato("Tasa promedio", fmt(r.tasa_promedio) + " Bs/USD") : null, filaDato("Depósitos", `$${fmt(r.depositos_total.usd)}`))];\n      kids.push(seccion("Por categoría", ...(r.arbol.length ? r.arbol.flatMap((n) => nodo(n, 0)) : [vacio("No hay gastos en este período.")])));\n      if (r.por_local.length) kids.push(seccion("Por local", ...r.por_local.map((l) => h("div", { class: "local-card" }, h("div", { class: "t1", text: `${l.icono} ${l.nombre}` }), filaDato("Gastos", `$${fmt(l.gastos.usd)}`), filaDato("Depósitos", `$${fmt(l.depositos.usd)}`), filaDato("Neto", `$${fmt(l.neto_usd)}`),\n        l.compartido_asignado ? filaDato("Parte de lo compartido", `$${fmt(l.compartido_asignado.usd)}`) : null, l.neto_con_compartido_usd != null ? filaDato("Neto con compartido", `$${fmt(l.neto_con_compartido_usd)}`) : null))));\n      kids.push(seccion("Compartido y personal", filaDato("Personal (gastos)", `$${fmt(r.grupos.personal.gastos.usd)}`), filaDato("Compartido NEGOCIOS (gastos)", `$${fmt(r.grupos.compartido.gastos.usd)}`), filaDato("Sin clasificar (gastos)", `$${fmt(r.grupos.sin_clasificar.gastos.usd)}`)));\n      salida.replaceChildren(...kids);\n    });\n    p.body.append(h("div", { class: "filtros glass" }, mes, anio, loc, h("label", { class: "campo check", for: "inf-rep" }, h("span", { text: "Repartir lo compartido entre los locales" }), rep), h("button", { class: "btn-s prim", type: "button", text: "Ver informe", onclick: cargar })), salida);\n    await cargar();\n  }\n\n  /* ---------- init ---------- */\n  function init() {\n    $("pin-btn").addEventListener("click", entrar);\n    $("nombre").addEventListener("keydown", (e) => { if (e.key === "Enter") $("pin").focus(); });\n    $("pin").addEventListener("keydown", (e) => { if (e.key === "Enter") entrar(); });\n    $("form").addEventListener("submit", (e) => { e.preventDefault(); const t = $("texto").value; $("texto").value = ""; enviar(t); });\n    $("btn-reporte").addEventListener("click", () => enviar("reporte", "Genera mi reporte de gastos"));\n    $("btn-config").addEventListener("click", panelConfig);\n    $("btn-historial").addEventListener("click", () => panelHistorial(false));\n    $("btn-facturas").addEventListener("click", () => panelFacturas("pendientes"));\n    $("btn-alertas").addEventListener("click", panelAlertas);\n    $("chips").addEventListener("click", (e) => {\n      const b = e.target.closest(".chip"); if (!b) return;\n      const i = $("texto"); i.value = b.dataset.prefijo; i.focus();\n    });\n    if (window.visualViewport) { window.visualViewport.addEventListener("resize", ajustarTeclado); window.visualViewport.addEventListener("scroll", ajustarTeclado); }\n    window.addEventListener("resize", ajustarTeclado);\n    window.addEventListener("orientationchange", ajustarTeclado);\n    ajustarTeclado();\n    $("nombre").focus();\n  }\n  function registrarSW() {\n    if (!("serviceWorker" in navigator)) return;\n    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => { /* la app funciona sin SW */ }));\n  }\n  registrarSW();\n  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();\n})();\n'.encode('utf-8'),
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
    if cfg["prod"] and cfg["pin_defecto"]:
        app_log.warning("APP_PIN no está definida: el PIN inicial es 1234. Cada usuario puede cambiarlo en Configuración.")
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
        if request.path != "/api/auth" and not (session.get("autenticado") and session.get("uid")):
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
        """Nombre + PIN. Nombre nuevo (con PIN inicial correcto) = registro, si hay cupo (máx. 2 usuarios)."""
        data = request.get_json(silent=True) or {}
        ip, ts = request.remote_addr or "?", ahora()
        with db.session() as s:
            espera = segundos_bloqueo(s, ip, ts)
            if espera:
                r = jsonify(ok=False, error=f"Demasiados intentos. Espera {math.ceil(espera / 60)} min.")
                r.status_code = 429
                r.headers["Retry-After"] = str(espera)
                return r
            try:
                nombre = nombre_valido(data.get("nombre"))
            except UsuarioError as e:
                return jsonify(ok=False, error=str(e)), 400
            u = buscar(s, nombre)
            if not pin_usuario_valido(data.get("pin"), u.pin_hash if u else None, cfg["pin"]):
                registrar_fallo(s, ip, ts)
                return jsonify(ok=False, error="PIN incorrecto"), 401
            nuevo = u is None
            if nuevo:
                try:
                    u = registrar(s, nombre)
                except UsuarioError as e:
                    return jsonify(ok=False, error=str(e)), 403
            limpiar(s, ip)
            session.clear()
            session["autenticado"] = True
            session["uid"] = u.id
            session.permanent = True
            tasa, ftasa, confirmada = estado_tasa(s, u.id, ts.date())
            completo = bool(tasa)
            botones = None
            if completo and confirmada:
                msg = f"{generar_saludo(u.nombre, ts.hour)} (Tasa de hoy: Bs. {tasa:,.2f})"
            elif completo:
                msg = (f"{generar_saludo(u.nombre, ts.hour)} La tasa de hoy aún no está confirmada "
                       f"(última: Bs. {tasa:,.2f} del {ftasa.strftime('%d/%m/%Y')}). ¿Cambió?")
                botones = botones_tasa(tasa)
            else:
                msg = f"¡Hola {u.nombre}! Por favor dime cuál es la tasa de cambio de hoy (ej: tasa 915)."
            return jsonify(ok=True, onboarding_completo=completo, mensaje=msg, nombre=u.nombre, nuevo=nuevo,
                           pregunta_tasa=botones is not None, botones=botones)

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    # ---------- chat ----------
    @app.post("/api/chat")
    def chat():
        body = request.get_json(silent=True) or {}
        elegir = body.get("elegir")
        forzado, texto = None, str(body.get("mensaje", "")).strip()
        if elegir is not None:
            pend = session.get("pend") or {}
            forzado = next((o for o in pend.get("opciones", []) if o["id"] == elegir), None) if isinstance(elegir, int) else None
            if forzado is None:
                session.pop("pend", None)
                return _txt("Esa opción ya no está disponible. Escribe el movimiento de nuevo.")
            texto = pend["mensaje"]
        if not texto:
            return _txt("Escribe un gasto o pide un reporte.")
        if len(texto) > 300:
            return _txt("Mensaje demasiado largo (máx. 300 caracteres).")
        with db.session() as s:
            payload, code = procesar(s, session["uid"], texto, ahora(), forzado)
        pend = payload.pop("_pend", None)
        if pend:
            session["pend"] = pend
        else:
            session.pop("pend", None)
        return jsonify(payload), code

    # ---------- resumen y presupuestos ----------
    @app.get("/api/resumen")
    def resumen():
        with db.session() as s:
            r = services_resumen(s, session["uid"], ahora().date())
            r["alertas"] = len(alertas(s, session["uid"], ahora().date()))   # número para el ícono (!)
            return jsonify(r)

    @app.put("/api/presupuestos")
    def presupuestos():
        d = request.get_json(silent=True) or {}
        cat = str(d.get("categoria", "")).strip().split(" ")[0][:60]
        lim = _dec(d.get("limite_usd", ""))
        if not cat or lim is None or lim < 0 or lim >= MAX_MONTO:
            return jsonify(error="Indica categoría y un límite en USD (0 para eliminar)"), 400
        with db.session() as s:
            c = resolver_o_crear(s, session["uid"], canon_categoria(cat))
            fijar_presupuesto(s, session["uid"], c.id, lim)
        return jsonify(ok=True)

    # ---------- facturas, pagos y alertas ----------
    def _fac(fn):
        """Ejecuta una operación de facturas y traduce FacturaError a JSON con su código HTTP."""
        try:
            with db.session() as s:
                return fn(s, session["uid"], ahora())
        except FacturaError as e:
            return jsonify(error=str(e)), e.codigo

    def _json():
        d = request.get_json(silent=True)
        return d if isinstance(d, dict) else {}

    @app.get("/api/facturas")
    def api_fac_listar():
        return _fac(lambda s, u, ts: jsonify(facturas=facturas_listar(
            s, u, ts.date(), request.args.get("estado", "todas"), (request.args.get("q") or "")[:60] or None)))

    @app.post("/api/facturas")
    def api_fac_crear():
        d = _json()
        return _fac(lambda s, u, ts: (jsonify(factura=facturas_crear(s, u, d, ts.date())), 201))

    @app.get("/api/facturas/<int:fid>")
    def api_fac_ver(fid):
        return _fac(lambda s, u, ts: jsonify(factura=obtener(s, u, fid, ts.date())))

    @app.put("/api/facturas/<int:fid>")
    def api_fac_editar(fid):
        d = _json()
        return _fac(lambda s, u, ts: jsonify(factura=facturas_editar(s, u, fid, d, ts.date())))

    @app.delete("/api/facturas/<int:fid>")
    def api_fac_borrar(fid):
        def op(s, u, ts):
            facturas_eliminar(s, u, fid, ts)
            return jsonify(ok=True)
        return _fac(op)

    @app.post("/api/facturas/<int:fid>/pagos")
    def api_fac_pagar(fid):
        d = _json()
        return _fac(lambda s, u, ts: (jsonify(factura=pagar(s, u, fid, d, ts.date(), ts)), 201))

    @app.delete("/api/facturas/<int:fid>/pagos/<int:aid>")
    def api_fac_anular_pago(fid, aid):
        return _fac(lambda s, u, ts: jsonify(factura=anular_abono(s, u, fid, aid, ts.date(), ts)))

    @app.post("/api/facturas/<int:fid>/restaurar")
    def api_fac_restaurar(fid):
        return _fac(lambda s, u, ts: jsonify(factura=facturas_restaurar(s, u, fid, ts.date())))

    @app.delete("/api/facturas/<int:fid>/definitivo")
    def api_fac_definitivo(fid):
        return _hist(lambda s, u, ts: jsonify(eliminar_factura_definitivo(s, u, fid)))

    @app.get("/api/alertas")
    def api_fac_alertas():
        def op(s, u, ts):
            a = alertas(s, u, ts.date())
            return jsonify(alertas=a, total=len(a))
        return _fac(op)

    @app.get("/api/preferencias")
    def api_pref_ver():
        return _fac(lambda s, u, ts: jsonify(preferencias(s, u)))

    @app.put("/api/preferencias")
    def api_pref_guardar():
        d = _json()
        return _fac(lambda s, u, ts: jsonify(guardar_preferencias(s, u, d)))

    # ---------- informe, historial y papelera ----------
    @app.get("/api/reporte")
    def api_reporte():
        def op(s, u, ts):
            a = request.args
            anio = _entero(a.get("anio"), "año", 2000, 2100, ts.year)
            mes = _entero(a.get("mes"), "mes", 1, 12, None) if a.get("mes") not in (None, "", "0") else None
            loc = a.get("local")
            filtro = int(loc) if loc and loc.isdigit() else (loc or None)
            try:
                rep = reporte(s, u, mes, anio, filtro, str(a.get("repartir", "0")).lower() in ("1", "true", "si"))
            except ArbolError as e:
                return jsonify(error=str(e)), 400
            return jsonify(reporte=rep)
        return _hist(op)

    def _hist(fn):
        try:
            with db.session() as s:
                return fn(s, session["uid"], ahora())
        except (HistorialError, FacturaError) as e:
            return jsonify(error=str(e)), e.codigo

    @app.get("/api/historial")
    def api_hist_listar():
        return _hist(lambda s, u, ts: jsonify(historial_listar(s, u, request.args)))

    @app.put("/api/movimientos/<int:mid>")
    def api_hist_editar(mid):
        d = _json()
        return _hist(lambda s, u, ts: jsonify(movimiento=historial_editar(s, u, mid, d)))

    @app.delete("/api/movimientos/<int:mid>")
    def api_hist_borrar(mid):
        return _hist(lambda s, u, ts: jsonify(borrar(s, u, mid, ts)))

    @app.post("/api/movimientos/<int:mid>/restaurar")
    def api_hist_restaurar(mid):
        return _hist(lambda s, u, ts: jsonify(historial_restaurar(s, u, mid)))

    @app.delete("/api/movimientos/<int:mid>/definitivo")
    def api_hist_definitivo(mid):
        return _hist(lambda s, u, ts: jsonify(eliminar_definitivo(s, u, mid)))

    @app.post("/api/papelera/vaciar")
    def api_hist_vaciar():
        return _hist(lambda s, u, ts: jsonify(vaciar_papelera(s, u)))

    # ---------- cuenta y categorías ----------
    @app.put("/api/nombre")
    def cambiar_nombre():
        try:
            with db.session() as s:
                u = usuarios_cambiar_nombre(s, session["uid"], (request.get_json(silent=True) or {}).get("nombre"))
                return jsonify(ok=True, nombre=u.nombre)
        except UsuarioError as e:
            return jsonify(error=str(e)), 400

    @app.put("/api/pin")
    def cambiar_pin():
        d = request.get_json(silent=True) or {}
        nuevo = str(d.get("nuevo", ""))
        if not re.fullmatch(r"\d{4,6}", nuevo):
            return jsonify(error="El PIN nuevo debe ser numérico de 4 a 6 dígitos."), 400
        ip, ts = request.remote_addr or "?", ahora()
        with db.session() as s:
            espera = segundos_bloqueo(s, ip, ts)
            if espera:
                return jsonify(error=f"Demasiados intentos. Espera {math.ceil(espera / 60)} min."), 429
            u = s.get(Usuario, session["uid"])
            if not pin_usuario_valido(d.get("actual"), u.pin_hash, cfg["pin"]):
                registrar_fallo(s, ip, ts)
                return jsonify(error="El PIN actual es incorrecto."), 401
            u.pin_hash = hash_pin(nuevo)
        return jsonify(ok=True)

    def _cfg(fn):
        """Operaciones de Configuración: traduce los errores de reglas a JSON (la transacción se revierte)."""
        try:
            with db.session() as s:
                return fn(s, session["uid"])
        except (ArbolError, PanelError, FacturaError) as e:
            msg = str(e)
            return jsonify(error=msg), (404 if msg.startswith("La categoría no existe") else 400)

    @app.get("/api/categorias")
    def categorias():
        return _cfg(lambda s, u: jsonify(arbol=arbol_json(s, u)))

    @app.post("/api/categorias")
    def api_cat_crear():
        d = _json()
        def op(s, u):
            pid = d.get("padre_id")
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise ArbolError("Elige dónde crear la categoría.")
            c = arbol_crear(s, u, d.get("nombre"), pid, d.get("icono"), bool(d.get("es_local")))
            return jsonify(id=c.id, arbol=arbol_json(s, u)), 201
        return _cfg(op)

    @app.put("/api/categorias/<int:cid>")
    def api_cat_editar(cid):
        d = _json()
        def op(s, u):
            if "nombre" in d:
                renombrar(s, u, cid, d["nombre"])
            if "icono" in d:
                cambiar_icono(s, u, cid, d["icono"])
            if "padre_id" in d:
                pid = d["padre_id"]
                if isinstance(pid, bool) or not isinstance(pid, int):
                    raise ArbolError("Elige dónde mover la categoría.")
                mover(s, u, cid, pid)
            if "es_local" in d:
                marcar_local(s, u, cid, bool(d["es_local"]))
            return jsonify(ok=True, arbol=arbol_json(s, u))
        return _cfg(op)

    @app.delete("/api/categorias/<int:cid>")
    def api_cat_borrar(cid):
        def op(s, u):
            dest = request.args.get("destino")
            if dest not in (None, "") and not dest.isdigit():
                raise ArbolError("Destino inválido.")
            arbol_eliminar(s, u, cid, int(dest) if dest else None)
            return jsonify(ok=True, arbol=arbol_json(s, u))
        return _cfg(op)

    @app.post("/api/categorias/<int:cid>/fusionar")
    def api_cat_fusionar(cid):
        d = _json()
        def op(s, u):
            dest = d.get("destino_id")
            if isinstance(dest, bool) or not isinstance(dest, int):
                raise ArbolError("Elige la categoría destino.")
            fusionar(s, u, cid, dest)
            return jsonify(ok=True, arbol=arbol_json(s, u))
        return _cfg(op)

    @app.get("/api/atajos")
    def api_atajos_ver():
        return _cfg(lambda s, u: jsonify(atajos=atajos(s, u), maximo=MAX_ATAJOS))

    @app.put("/api/atajos")
    def api_atajos_guardar():
        d = _json()
        return _cfg(lambda s, u: jsonify(atajos=guardar_atajos(s, u, d.get("categorias"))))

    @app.get("/api/ajustes")
    def api_ajustes_ver():
        return _cfg(lambda s, u: jsonify(saldo_inicial_usd=saldo_inicial(s, u)))

    @app.put("/api/ajustes")
    def api_ajustes_guardar():
        d = _json()
        return _cfg(lambda s, u: jsonify(saldo_inicial_usd=guardar_saldo_inicial(s, u, d.get("saldo_inicial_usd"))))

    # ---------- exportar ----------
    @app.get("/api/exportar")
    def exportar():
        hoy = ahora().date()
        with db.session() as s:
            filas = list(reversed(
                [m for t in ("gasto", "ingreso", "deposito") for m in movimientos(s, session["uid"], t, *rango(hoy.month, hoy.year))]))
            cats = cargar(s, session["uid"])
            filas.sort(key=lambda m: (m.fecha, m.id))
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["Fecha", "Tipo", "Categoria", "Descripcion", "Monto_Bs", "Tasa_Aplicada", "Monto_USD", "Ruta"])
            for m in filas:
                w.writerow([m.fecha.isoformat(), m.tipo, _csv_seguro(m.categoria), _csv_seguro(m.descripcion or ""),
                            f"{m.monto_bs:.2f}", f"{m.tasa_aplicada:.4f}", f"{m.monto_usd:.2f}",
                            _csv_seguro(" > ".join(arbol_ruta(cats, m.categoria_id)))])
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

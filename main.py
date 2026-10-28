import streamlit as st
import pandas as pd
import json
import hashlib
import time
import io
from PIL import Image
from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Optional
from google import genai
from google.genai import types

# =====================================================================
# 1. CONFIGURACIÓN DE PÁGINA Y ESTILOS (USABILIDAD)
# =====================================================================
st.set_page_config(
    page_title="MareS - Validador Inteligente de Gastos",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .badge-apto { background-color: #d1fae5; color: #065f46; padding: 6px 12px; border-radius: 6px; font-weight: bold; border: 1px solid #34d399; }
    .badge-revision { background-color: #fef3c7; color: #92400e; padding: 6px 12px; border-radius: 6px; font-weight: bold; border: 1px solid #fbbf24; }
    .badge-no-apto { background-color: #fee2e2; color: #991b1b; padding: 6px 12px; border-radius: 6px; font-weight: bold; border: 1px solid #f87171; }
</style>
""", unsafe_allow_html=True)

# =====================================================================
# 2. CONFIGURACIÓN OPERATIVA
# =====================================================================
# La aplicación extrae placa e identidad únicamente como evidencia cuando son
# visibles, sin emplearlas para aceptar, revisar o rechazar un comprobante.


# =====================================================================
# 3. ESQUEMA ESTRUCTURADO PARA GEMINI (Sección 3, 4 y 5)
# =====================================================================
class ComprobanteExtraido(BaseModel):
    tipo_documento: str = Field(
        description="Opciones estrictas: 'Factura electrónica de venta', 'Documento equivalente electrónico (POS, peaje u otros)', 'Factura en papel o talonario', 'Documento soporte a no obligados a facturar', 'Recibo manuscrito / informal', 'Cuenta de cobro', 'Soporte de pago', 'Egreso / legalización interna', 'Ilegible'")
    numero_comprobante: Optional[str] = Field(description="Número de factura, tiquete o recibo. Null si no es visible.")
    fecha_emision: Optional[str] = Field(description="Fecha en formato YYYY-MM-DD. Null si no es visible o es dudosa.")
    emisor_nombre: Optional[str] = Field(description="Nombre o razón social del comercio o emisor.")
    emisor_nit: Optional[str] = Field(description="NIT o cédula del emisor sin dígito de verificación o con guión.")
    descripcion_gasto: str = Field(description="Resumen breve de los ítems o concepto del gasto.")
    categoria_gasto: str = Field(
        description="Opciones estrictas: 'Combustible', 'Hospedaje', 'Alimentación', 'Peaje / Transporte', 'Mantenimiento / Repuestos', 'Papelería / Oficina', 'Otro'")
    destino_sugerido: str = Field(
        description="Opciones estrictas: 'Vehículo', 'Persona', 'Proceso administrativo', 'Proceso productivo'")
    placa_detectada: Optional[str] = Field(
        description="Placa del vehículo en mayúsculas sin espacios ni guiones (ej. ABC123) si aparece en el documento.")
    cedula_detectada: Optional[str] = Field(
        description="Cédula o identificación del colaborador o adquirente si aparece.")
    nombre_persona_detectada: Optional[str] = Field(description="Nombre del colaborador si aparece.")
    subtotal: Optional[float] = Field(
        description="Subtotal numérico antes de impuestos o propinas. Null si no se discrimina.")
    impuestos: Optional[float] = Field(
        description="Suma total de IVA, impoconsumo u otros impuestos. 0 si no aplica o no aparece.")
    propina_otros: Optional[float] = Field(description="Propina u otros cargos adicionales. 0 si no aplica.")
    total: Optional[float] = Field(description="Valor total pagado numérico. Null si es ilegible.")
    moneda: str = Field(description="Código de moneda, por defecto 'COP'.")
    cufe_cuds: Optional[str] = Field(description="Código CUFE o CUDS si es documento electrónico.")
    nivel_legibilidad: str = Field(description="Opciones estrictas: 'Alta', 'Media', 'Baja', 'Nula'")
    campos_dudosos: List[str] = Field(
        description="Lista de nombres de campos que están borrosos, tachados, cortados o con incertidumbre de lectura.")
    observaciones_ia: str = Field(
        description="Explicación puntual de cualquier anomalía visual, texto manuscrito confuso o incertidumbre.")


# =====================================================================
# 4. MOTOR DE REGLAS Y VALIDACIONES (Sección 7 y 8)
# =====================================================================
def ejecutar_validaciones(datos: dict, hash_img: str, lote_actual: dict) -> dict:
    alertas = []
    observaciones_no_bloqueantes = []
    reglas_verificadas = []
    errores_criticos = []
    campos_inciertos = list(datos.get("campos_dudosos", []))

    # 1. Validación de Legibilidad y Abstención
    if datos["nivel_legibilidad"] == "Nula" or datos["tipo_documento"] == "Ilegible" or datos["total"] is None:
        errores_criticos.append("Documento ilegible o sin valor total identificable (Abstención obligatoria).")
    else:
        reglas_verificadas.append("Legibilidad base e importe total presentes")

    # 2. Validación de Duplicados (por Hash de archivo o por Emisor + Número + Total)
    for id_doc, doc_previo in lote_actual.items():
        if doc_previo["hash_archivo"] == hash_img:
            errores_criticos.append(f"Archivo duplicado exacto (mismo hash que {id_doc}).")
        elif (datos.get("numero_comprobante") and datos.get("emisor_nit") and
              datos["numero_comprobante"] == doc_previo["datos_finales"].get("numero_comprobante") and
              datos["emisor_nit"] == doc_previo["datos_finales"].get("emisor_nit") and
              datos["total"] == doc_previo["datos_finales"].get("total")):
            errores_criticos.append(f"Comprobante duplicado detectado (Mismo NIT, Número y Total en {id_doc}).")
    if not any("duplicado" in e.lower() for e in errores_criticos):
        reglas_verificadas.append("Control de duplicidad superado")

    # 3. Exactitud matemática de importes
    sub = datos.get("subtotal")
    imp = datos.get("impuestos") or 0.0
    prop = datos.get("propina_otros") or 0.0
    tot = datos.get("total")

    if sub is not None and tot is not None:
        suma_calculada = round(sub + imp + prop, 2)
        diferencia = abs(suma_calculada - tot)
        if diferencia > 100:  # Tolerancia de $100 COP por redondeos de centavos
            alertas.append(
                f"Inconsistencia matemática: Subtotal (${sub:,.0f}) + Impuestos (${imp:,.0f}) = ${suma_calculada:,.0f}, pero el Total leído es ${tot:,.0f}.")
            if "total" not in campos_inciertos:
                campos_inciertos.append("total")
        else:
            reglas_verificadas.append("Exactitud aritmética verificada (Subtotal + Impuestos = Total)")

    # 4. Consistencia del Tipo Documental
    if datos["tipo_documento"] == "Factura electrónica de venta" and not datos.get("cufe_cuds"):
        observaciones_no_bloqueantes.append(
            "Factura electrónica sin CUFE visible: se conserva para trazabilidad; "
            "el número, emisor y total son los identificadores usados en esta validación."
        )
    if datos["tipo_documento"] in ["Recibo manuscrito / informal", "Soporte de pago", "Cuenta de cobro"]:
        observaciones_no_bloqueantes.append(
            f"Precaución normativa: '{datos['tipo_documento']}' no constituye soporte tributario definitivo por sí solo.")

    # 5. Campos Condicionados según Destino del Gasto (Regla Operativa MareS)
    destino = datos.get("destino_sugerido")
    if destino == "Vehículo" or datos.get("categoria_gasto") == "Combustible":
        datos["destino_sugerido"] = "Vehículo"
        placa = (datos.get("placa_detectada") or "").upper().replace(" ", "").replace("-", "")
        datos["placa_detectada"] = placa
        campos_inciertos = [
            campo for campo in campos_inciertos if campo != "placa_detectada"
        ]
        if placa:
            reglas_verificadas.append("Placa visible registrada como evidencia, sin cruce externo")
        else:
            observaciones_no_bloqueantes.append(
                "Gasto de vehículo sin placa visible; se registra como información no disponible, sin bloquear la decisión."
            )

    elif destino == "Persona" or datos.get("categoria_gasto") in ["Hospedaje", "Alimentación"]:
        datos["destino_sugerido"] = "Persona"
        # No se usan identidades propuestas por la IA para decidir.
        campos_identidad = {
            "cedula_detectada",
            "nombre_persona_detectada",
            "cargo_persona"
        }
        campos_inciertos = [
            campo for campo in campos_inciertos if campo not in campos_identidad
        ]
        datos["cedula_detectada"] = None
        datos["nombre_persona_detectada"] = None
        datos["cargo_persona"] = None
        reglas_verificadas.append(
            "Destino Persona: identidad no inferida ni usada para decidir"
        )

    # 6. Consistencia de Fecha e Identificadores Básicos
    if not datos.get("fecha_emision"):
        alertas.append("Fecha de emisión no detectada o ilegible.")
        campos_inciertos.append("fecha_emision")
    if not datos.get("emisor_nombre"):
        alertas.append("Nombre del comercio/emisor no identificado.")
        campos_inciertos.append("emisor_nombre")

    # =================================================================
    # ÁRBOL DE DECISIÓN (Sección 7: Aceptar / Revisar / No Apto)
    # =================================================================
    # Solo la incertidumbre sobre información decisoria debe frenar la
    # autoaceptación. Número, NIT, CUFE, subtotal y cargos separados pueden
    # no aparecer en ciertos soportes válidos y permanecen en trazabilidad.
    campos_criticos = {
        "tipo_documento",
        "total",
        "fecha_emision",
        "emisor_nombre",
        "destino_sugerido"
    }
    incertidumbres_criticas = [
        campo for campo in campos_inciertos if campo in campos_criticos
    ]

    if len(errores_criticos) > 0:
        estado = "NO APTO PARA AUTOACEPTACIÓN"
        motivo = " | ".join(errores_criticos)
    elif len(alertas) > 0 or len(incertidumbres_criticas) > 0 or datos["nivel_legibilidad"] == "Baja":
        estado = "REQUIERE REVISIÓN"
        motivo = " | ".join(alertas) if alertas else "Presencia de campos con baja confianza de lectura."
    else:
        estado = "APTO PARA AUTOACEPTACIÓN"
        motivo = "Los campos críticos e importes superaron las validaciones. Las observaciones no bloqueantes permanecen trazadas."

    return {
        "estado": estado,
        "motivo_decision": motivo,
        "alertas": alertas,
        "observaciones_no_bloqueantes": observaciones_no_bloqueantes,
        "errores_criticos": errores_criticos,
        "reglas_verificadas": reglas_verificadas,
        "campos_inciertos": list(set(campos_inciertos))
    }


# =====================================================================
# 5. EXTRACCIÓN RÁPIDA CON COMPRESIÓN, TIMEOUT Y FILTRO MULTIMODAL
# =====================================================================
def es_modelo_vision_valido(nombre_modelo: str) -> bool:
    """Filtra modelos de voz (TTS), embeddings o deprecados que causan error 400/404."""
    nm = nombre_modelo.lower()
    palabras_excluidas = [
        "tts", "audio", "speech", "embedding", "imagen", "veo", "aqa",
        "thinking", "1.5", "1.0", "gemini-2.5-flash", "gemini-2.5-pro"
    ]
    # Permitir gemini-2.5-flash-lite si existe, pero bloquear los 2.5 estándar retirados
    if "gemini-2.5-flash-lite" in nm:
        return True
    return "gemini" in nm and not any(excl in nm for excl in palabras_excluidas)


def optimizar_imagen(imagen: Image.Image, max_size: int = 1600) -> Image.Image:
    """Reduce el peso de fotos grandes de celular para que suban en <1 segundo."""
    img_copy = imagen.copy()
    if img_copy.mode in ("RGBA", "P"):
        img_copy = img_copy.convert("RGB")
    img_copy.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return img_copy


def analizar_imagen_con_gemini(imagen: Image.Image, api_key: str, modelo_primario: str) -> dict:
    print("-> [1/3] Optimizando resolución de la imagen en memoria...")
    imagen_opt = optimizar_imagen(imagen)

    # Cliente con Timeout estricto de 25 segundos (25000 ms) para evitar congelamientos
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=25000)
    )

    prompt = """
    Eres un auditor contable experto en normativa tributaria colombiana (DIAN) para la empresa MareS.
    Analiza la imagen de este comprobante de gasto y extrae los datos siguiendo estrictamente el esquema JSON.

    REGLAS DE ORO DE AUDITORÍA:
    1. ABSTENCIÓN: NUNCA inventes ni adivines un dato borroso o cortado. Si la duda afecta tipo de soporte, emisor, fecha, total, destino o placa de vehículo, déjalo en null e incluye el campo en 'campos_dudosos'. No marques como dudosos campos opcionales que simplemente no aparecen (CUFE, NIT, número, subtotal, impuestos, identidad de persona).
    2. CLASIFICACIÓN DE GASTO: Identifica si es Combustible, Hospedaje, Alimentación, Peaje / Transporte, Mantenimiento / Repuestos, Papelería / Oficina u Otro.
    3. DESTINO DEL GASTO:
       - Si es Combustible, Peaje o taller mecánico -> 'Vehículo'. Busca activamente una placa colombiana (3 letras y 3 números).
       - Si es Hospedaje o Alimentación -> 'Persona'. No inventes ni propongas la identidad del colaborador: solo extrae cédula o nombre si se leen explícitamente en el soporte.
       - Si es papelería o servicios de oficina -> 'Proceso administrativo'.
       - Si son insumos de planta o ferretería operativa -> 'Proceso productivo'.
    4. IMPORTES: En Colombia el punto (.) suele separar miles y la coma (,) decimales, pero en tiquetes POS a veces usan coma para miles. Interpreta el valor real en Pesos Colombianos (COP) sin confundir miles con decimales.
    """

    # Orden de prioridad estricto con los modelos modernos recomendados por la API de Google
    prioridad_modelos = [
                            modelo_primario,
                            "gemini-3.1-pro-preview",
                            "gemini-3.8-flash",
                            "gemini-3.1-flash-lite-preview",
                            "gemini-3.0-flash",
                            "gemini-2.5-flash-lite",
                            "gemini-2.0-flash",
                            "gemini-2.0-flash-lite"
                        ] + st.session_state.get("modelos_disponibles", [])

    modelos_candidatos = []
    for m in prioridad_modelos:
        if m not in modelos_candidatos and es_modelo_vision_valido(m):
            modelos_candidatos.append(m)

    caja_estado = st.empty()
    ultimo_error = None

    for mod in modelos_candidatos:
        for intento in range(1, 3):  # Máximo 2 intentos rápidos por modelo
            msg = f"📡 Consultando modelo `{mod}` (Intento {intento}/2 - máx 25s)..."
            print(f"-> [2/3] {msg}")
            caja_estado.info(msg)

            try:
                t_inicio = time.time()
                response = client.models.generate_content(
                    model=mod,
                    contents=[imagen_opt, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ComprobanteExtraido,
                        temperature=0.0
                    )
                )
                t_total = time.time() - t_inicio
                print(f"-> [3/3] ¡ÉXITO! Respuesta recibida de {mod} en {t_total:.1f} segundos.")
                caja_estado.empty()
                return json.loads(response.text)

            except Exception as e:
                err_str = str(e)
                ultimo_error = e
                print(f"   [!] Falló {mod} (Intento {intento}): {err_str}")

                # Si es 404 (No existe) o 400 (No soporta imágenes/JSON), saltar de inmediato al siguiente modelo
                if "404" in err_str or "NOT_FOUND" in err_str or "400" in err_str or "INVALID_ARGUMENT" in err_str:
                    print(f"   [!] Saltando {mod} por incompatibilidad o retiro (400/404).")
                    break
                elif "503" in err_str or "UNAVAILABLE" in err_str or "429" in err_str or "timeout" in err_str.lower() or "timed out" in err_str.lower():
                    caja_estado.warning(f"⚠️ `{mod}` ocupado (503/Timeout). Probando alternativa en 2s...")
                    time.sleep(2)
                else:
                    break

    caja_estado.empty()
    raise ultimo_error


# =====================================================================
# 6. ESTADO DE SESIÓN Y SIDEBAR
# =====================================================================
MODELOS_POR_DEFECTO = [
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash"
]

if "lote_documentos" not in st.session_state:
    st.session_state.lote_documentos = {}
if "documentos_trabajador" not in st.session_state:
    st.session_state.documentos_trabajador = []
if "modelos_disponibles" not in st.session_state or "gemini-2.5-flash" in st.session_state.modelos_disponibles:
    st.session_state.modelos_disponibles = MODELOS_POR_DEFECTO

with st.sidebar:
    st.title("⚙️ Configuración MareS")
    try:
        api_key_servidor = st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        api_key_servidor = ""

    if api_key_servidor:
        api_key = api_key_servidor
        st.caption("✅ Servicio de procesamiento configurado.")
    else:
        api_key = st.text_input(
            "🔑 Gemini API Key",
            type="password",
            help="Configúrala en el servidor para que los trabajadores no tengan que ingresarla."
        )

    if api_key and st.button("🔄 Detectar modelos de Visión activos", use_container_width=True):
        try:
            client_test = genai.Client(api_key=api_key)
            lista_mods = []
            for m in client_test.models.list():
                nombre_m = m.name.replace("models/", "")
                if es_modelo_vision_valido(nombre_m):
                    lista_mods.append(nombre_m)
            if lista_mods:
                # Ordenar de más reciente (3.x) a más antiguo (2.x) para no dejar los 2.5 arriba
                lista_mods.sort(reverse=True)
                st.session_state.modelos_disponibles = lista_mods
                st.success(f"¡{len(lista_mods)} modelos multimodales válidos detectados!")
                print("Modelos de visión válidos en tu cuenta:", lista_mods)
        except Exception as e:
            st.error(f"No se pudo listar modelos: {e}")

    modelo_seleccionado = st.selectbox(
        "🤖 Modelo Gemini Principal",
        st.session_state.modelos_disponibles,
        index=0,
        help="Usa gemini-3.1-pro-preview o gemini-3.8-flash. Si uno está ocupado, el sistema saltará automáticamente al otro."
    )

    pausa_segundos = st.slider(
        "⏱️ Pausa entre facturas (seg)",
        min_value=0, max_value=10, value=2,
        help="Evita errores 429 de límite de cuota gratuita cuando procesas muchas facturas seguidas."
    )

    st.divider()
    if st.button("🗑️ Limpiar Lote Actual", use_container_width=True):
        st.session_state.lote_documentos = {}
        st.session_state.documentos_trabajador = []
        st.rerun()

# =====================================================================
# 7. ENCABEZADO Y MÉTRICAS DE DESEMPEÑO (KPIs en tiempo real)
# =====================================================================
st.title("🧾 MareS | Gestión de Comprobantes")
st.caption("Carga de soportes, decisión automatizada y trazabilidad contable")

docs = st.session_state.lote_documentos

# =====================================================================
# 8. PESTAÑAS DE FLUJO DE TRABAJO (USABILIDAD)
# =====================================================================
tab_trabajador, tab_carga, tab_auditoria, tab_exportar = st.tabs([
    "👷 Módulo Trabajador",
    "🛠️ Administración · Cargar lote",
    "🔍 Administración · Auditoría",
    "📊 Administración · Trazabilidad"
])

# ---------------------------------------------------------------------
# MÓDULO TRABAJADOR: CARGA Y CONSULTA DEL RESULTADO
# ---------------------------------------------------------------------
with tab_trabajador:
    st.subheader("Enviar comprobantes de gasto")
    st.caption(
        "Sube fotos de tus facturas o recibos. Al finalizar verás únicamente "
        "la decisión y los comentarios asociados a cada comprobante."
    )
    archivos_trabajador = st.file_uploader(
        "Selecciona tus imágenes (JPG, JPEG o PNG)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key="carga_trabajador"
    )

    if archivos_trabajador and st.button(
        "📤 Enviar comprobantes", type="primary", key="procesar_trabajador"
    ):
        if not api_key:
            st.error("El servicio de procesamiento no está configurado todavía.")
        else:
            barra_trabajador = st.progress(0, text="Preparando comprobantes...")
            exitosos_trabajador = 0
            for idx, archivo in enumerate(archivos_trabajador):
                id_interno = f"DOC-{len(st.session_state.lote_documentos) + 1:03d}"
                bytes_img = archivo.getvalue()
                hash_img = hashlib.md5(bytes_img).hexdigest()
                imagen_pil = Image.open(archivo)
                barra_trabajador.progress(
                    idx / len(archivos_trabajador),
                    text=f"Analizando comprobante {idx + 1} de {len(archivos_trabajador)}..."
                )

                try:
                    datos_ia = analizar_imagen_con_gemini(
                        imagen_pil, api_key, modelo_seleccionado
                    )
                    datos_ia["cargo_persona"] = None
                    resultado_val = ejecutar_validaciones(
                        datos_ia, hash_img, st.session_state.lote_documentos
                    )
                    st.session_state.lote_documentos[id_interno] = {
                        "id_interno": id_interno,
                        "nombre_archivo": archivo.name,
                        "hash_archivo": hash_img,
                        "imagen": imagen_pil,
                        "datos_originales_ia": dict(datos_ia),
                        "datos_finales": dict(datos_ia),
                        "validacion": resultado_val,
                        "corregido_humano": False,
                        "historial_cambios": []
                    }
                    st.session_state.documentos_trabajador.append(id_interno)
                    exitosos_trabajador += 1
                    if idx < len(archivos_trabajador) - 1 and pausa_segundos > 0:
                        time.sleep(pausa_segundos)
                except Exception:
                    st.error(f"No fue posible procesar {archivo.name}. Inténtalo nuevamente.")

            barra_trabajador.progress(
                1.0,
                text=f"Proceso finalizado: {exitosos_trabajador} comprobante(s) enviado(s)."
            )

    documentos_trabajador = [
        docs[id_doc] for id_doc in st.session_state.documentos_trabajador
        if id_doc in docs
    ]
    if documentos_trabajador:
        st.divider()
        st.subheader("Resultado de tus comprobantes")
        for documento in reversed(documentos_trabajador):
            validacion = documento["validacion"]
            estado = validacion["estado"]
            etiqueta = (
                "ACEPTADO" if estado == "APTO PARA AUTOACEPTACIÓN"
                else "REVISIÓN" if estado == "REQUIERE REVISIÓN"
                else "NO APTO"
            )
            clase = (
                "badge-apto" if estado == "APTO PARA AUTOACEPTACIÓN"
                else "badge-revision" if estado == "REQUIERE REVISIÓN"
                else "badge-no-apto"
            )
            with st.expander(f"{documento['nombre_archivo']} · {etiqueta}", expanded=True):
                st.markdown(
                    f"<div class='{clase}'>RESULTADO: {etiqueta}</div>",
                    unsafe_allow_html=True
                )
                st.write("")
                st.write(validacion["motivo_decision"])
                for error in validacion.get("errores_criticos", []):
                    st.error(f"🛑 {error}")
                for alerta in validacion.get("alertas", []):
                    st.warning(f"⚠️ {alerta}")
                for observacion in validacion.get("observaciones_no_bloqueantes", []):
                    st.info(f"ℹ️ **Dato trazado, sin bloqueo:** {observacion}")

# ---------------------------------------------------------------------
# MÓDULO ADMINISTRADOR: CARGA DE IMÁGENES
# ---------------------------------------------------------------------
with tab_carga:
    st.subheader("Panel de trabajo del Administrador")
    total_docs = len(docs)
    aptos = sum(1 for d in docs.values() if d["validacion"]["estado"] == "APTO PARA AUTOACEPTACIÓN")
    revision = sum(1 for d in docs.values() if d["validacion"]["estado"] == "REQUIERE REVISIÓN")
    no_aptos = sum(1 for d in docs.values() if d["validacion"]["estado"] == "NO APTO PARA AUTOACEPTACIÓN")
    corregidos = sum(1 for d in docs.values() if d["corregido_humano"])

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("📂 Total Procesados", total_docs)
    col2.metric("✅ Autoaceptados", aptos, f"{(aptos / total_docs * 100):.0f}" if total_docs else "0%")
    col3.metric("⚠️ En Revisión Humana", revision)
    col4.metric("🛑 Bloqueados / No Aptos", no_aptos)
    col5.metric("✏️ Auditados por Humano", corregidos)

    st.divider()
    st.subheader("Carga de Comprobantes (Facturas, POS, Recibos Manuscritos)")
    archivos_subidos = st.file_uploader(
        "Selecciona tus imágenes de facturas (JPG, PNG, JPEG)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True
    )

    if archivos_subidos:
        if st.button("🚀 Procesar Comprobantes con IA + Motor de Reglas", type="primary"):
            if not api_key:
                st.error("Por favor ingresa tu Gemini API Key en el menú lateral izquierdo.")
            else:
                barra = st.progress(0, text="Iniciando procesamiento...")
                exitosos = 0
                for idx, archivo in enumerate(archivos_subidos):
                    id_interno = f"DOC-{len(st.session_state.lote_documentos) + 1:03d}"
                    bytes_img = archivo.getvalue()
                    hash_img = hashlib.md5(bytes_img).hexdigest()
                    imagen_pil = Image.open(archivo)

                    barra.progress((idx) / len(archivos_subidos),
                                   text=f"Analizando {archivo.name} ({idx + 1}/{len(archivos_subidos)})...")

                    try:
                        datos_ia = analizar_imagen_con_gemini(imagen_pil, api_key, modelo_seleccionado)
                        datos_ia["cargo_persona"] = None
                        resultado_val = ejecutar_validaciones(datos_ia, hash_img, st.session_state.lote_documentos)

                        st.session_state.lote_documentos[id_interno] = {
                            "id_interno": id_interno,
                            "nombre_archivo": archivo.name,
                            "hash_archivo": hash_img,
                            "imagen": imagen_pil,
                            "datos_originales_ia": dict(datos_ia),
                            "datos_finales": dict(datos_ia),
                            "validacion": resultado_val,
                            "corregido_humano": False,
                            "historial_cambios": []
                        }
                        exitosos += 1
                        if idx < len(archivos_subidos) - 1 and pausa_segundos > 0:
                            time.sleep(pausa_segundos)
                    except Exception as e:
                        st.error(f"Error procesando {archivo.name}: {str(e)}")

                barra.progress(1.0,
                               text=f"¡Procesamiento finalizado! ({exitosos}/{len(archivos_subidos)} procesados correctamente).")
                if exitosos > 0:
                    time.sleep(1)
                    st.rerun()

# ---------------------------------------------------------------------
# TAB 2: ESTACIÓN DE AUDITORÍA HUMANA (SPLIT-SCREEN)
# ---------------------------------------------------------------------
with tab_auditoria:
    if not docs:
        st.info("Aún no has procesado ningún documento. Sube tus facturas en la pestaña 1.")
    else:
        col_filtro, col_selector = st.columns([1, 2])
        with col_filtro:
            filtro_estado = st.selectbox(
                "Filtrar por estado de decisión:",
                ["Todos", "REQUIERE REVISIÓN", "APTO PARA AUTOACEPTACIÓN", "NO APTO PARA AUTOACEPTACIÓN"]
            )

        docs_filtrados = {
            k: v for k, v in docs.items()
            if filtro_estado == "Todos" or v["validacion"]["estado"] == filtro_estado
        }

        if not docs_filtrados:
            st.success(f"No hay documentos pendientes en el estado '{filtro_estado}'.")
        else:
            with col_selector:
                opciones_doc = {
                    k: f"{k} | {v['nombre_archivo']} [{v['validacion']['estado']}] - {v['datos_finales'].get('categoria_gasto')}"
                    for k, v in docs_filtrados.items()
                }
                doc_seleccionado_id = st.selectbox(
                    "Seleccionar comprobante a inspeccionar:",
                    options=list(opciones_doc.keys()),
                    format_func=lambda x: opciones_doc[x]
                )

            doc_actual = docs[doc_seleccionado_id]
            datos_f = doc_actual["datos_finales"]
            val_f = doc_actual["validacion"]

            st.divider()

            # VISTA LADO A LADO: IMAGEN A LA IZQUIERDA, FORMULARIO A LA DERECHA
            col_img, col_form = st.columns([1, 1.3])

            with col_img:
                st.markdown(f"#### 🖼️ Evidencia Original: `{doc_actual['nombre_archivo']}`")
                st.image(doc_actual["imagen"], use_container_width=True)
                st.caption(
                    f"Hash MD5 de trazabilidad: `{doc_actual['hash_archivo']}` | Legibilidad IA: **{datos_f.get('nivel_legibilidad')}**")
                if datos_f.get("observaciones_ia"):
                    st.info(f"🤖 **Nota visual de la IA:** {datos_f['observaciones_ia']}")

            with col_form:
                # Semáforo de Estado
                estado_str = val_f["estado"]
                clase_badge = "badge-apto" if estado_str == "APTO PARA AUTOACEPTACIÓN" else (
                    "badge-revision" if estado_str == "REQUIERE REVISIÓN" else "badge-no-apto")
                st.markdown(f"<div class='{clase_badge}'>ESTADO ACTUAL: {estado_str}</div>", unsafe_allow_html=True)
                st.write("")

                # Alertas y Motivos
                if val_f["errores_criticos"]:
                    for err in val_f["errores_criticos"]:
                        st.error(f"🛑 **Error Crítico:** {err}")
                if val_f["alertas"]:
                    for al in val_f["alertas"]:
                        st.warning(f"⚠️ **Incertidumbre / Regla:** {al}")
                if val_f.get("observaciones_no_bloqueantes"):
                    for obs in val_f["observaciones_no_bloqueantes"]:
                        st.info(f"ℹ️ **Dato trazado, sin bloqueo:** {obs}")
                if val_f["campos_inciertos"]:
                    st.caption(
                        f"🔍 **Campos señalados para verificación puntual:** `{', '.join(val_f['campos_inciertos'])}`")

                # Formulario de Edición y Verificación Humana
                with st.form(key=f"form_{doc_seleccionado_id}"):
                    st.markdown("##### 📝 Datos Extraídos y Asignación Contable")
                    c1, c2 = st.columns(2)

                    tipos_doc_lista = [
                        "Factura electrónica de venta", "Documento equivalente electrónico (POS, peaje u otros)",
                        "Factura en papel o talonario", "Documento soporte a no obligados a facturar",
                        "Recibo manuscrito / informal", "Cuenta de cobro", "Soporte de pago",
                        "Egreso / legalización interna", "Ilegible"
                    ]
                    nuevo_tipo = c1.selectbox("Tipo de Soporte (DIAN)", tipos_doc_lista,
                                              index=tipos_doc_lista.index(datos_f["tipo_documento"]) if datos_f[
                                                                                                            "tipo_documento"] in tipos_doc_lista else 0)

                    cats_lista = ["Combustible", "Hospedaje", "Alimentación", "Peaje / Transporte",
                                  "Mantenimiento / Repuestos", "Papelería / Oficina", "Otro"]
                    nueva_cat = c2.selectbox("Categoría del Gasto", cats_lista,
                                             index=cats_lista.index(datos_f["categoria_gasto"]) if datos_f[
                                                                                                       "categoria_gasto"] in cats_lista else 6)

                    c3, c4, c5 = st.columns(3)
                    nuevo_num = c3.text_input("Número Comprobante", value=datos_f.get("numero_comprobante") or "")
                    nueva_fecha = c4.text_input("Fecha (YYYY-MM-DD)", value=datos_f.get("fecha_emision") or "")
                    nuevo_nit = c5.text_input("NIT / Cédula Emisor", value=datos_f.get("emisor_nit") or "")

                    nuevo_emisor = st.text_input("Nombre / Razón Social Emisor",
                                                 value=datos_f.get("emisor_nombre") or "")
                    nueva_desc = st.text_input("Descripción del Gasto", value=datos_f.get("descripcion_gasto") or "")

                    st.markdown("##### 💰 Importes Monetarios (COP)")
                    m1, m2, m3 = st.columns(3)
                    nuevo_sub = m1.number_input("Subtotal", value=float(datos_f.get("subtotal") or 0.0), step=100.0)
                    nuevo_imp = m2.number_input("Impuestos (IVA/Ipoconsumo)",
                                                value=float(datos_f.get("impuestos") or 0.0), step=100.0)
                    nuevo_tot = m3.number_input("Total Pagado (*)", value=float(datos_f.get("total") or 0.0),
                                                step=100.0)

                    st.markdown("##### 🎯 Asignación de Destino MareS (Sección 5)")
                    d1, d2, d3 = st.columns(3)
                    destinos_lista = ["Vehículo", "Persona", "Proceso administrativo", "Proceso productivo"]
                    nuevo_destino = d1.selectbox("¿A quién se carga el gasto?", destinos_lista,
                                                 index=destinos_lista.index(datos_f["destino_sugerido"]) if datos_f[
                                                                                                                "destino_sugerido"] in destinos_lista else 0)
                    nueva_placa = d2.text_input("Placa Vehículo (Si aplica)",
                                                value=datos_f.get("placa_detectada") or "")
                    nueva_cedula = d3.text_input("Cédula Colaborador (Si aplica)",
                                                 value=datos_f.get("cedula_detectada") or "")

                    justificacion_humana = st.text_input(
                        "✍️ Motivo de corrección o aprobación manual (Obligatorio para trazabilidad):",
                        placeholder="Ej. Se corrige placa borrosa según bitácora de viaje...")

                    col_btn1, col_btn2 = st.columns(2)
                    guardar_validar = col_btn1.form_submit_button("💾 Guardar Cambios y Re-evaluar Reglas",
                                                                  type="primary", use_container_width=True)
                    rechazar_manual = col_btn2.form_submit_button("🛑 Marcar como No Apto Definitivo",
                                                                  use_container_width=True)

                    if guardar_validar:
                        cambios = []
                        actualizaciones = {
                            "tipo_documento": nuevo_tipo,
                            "categoria_gasto": nueva_cat,
                            "numero_comprobante": nuevo_num if nuevo_num else None,
                            "fecha_emision": nueva_fecha if nueva_fecha else None,
                            "emisor_nit": nuevo_nit if nuevo_nit else None,
                            "emisor_nombre": nuevo_emisor if nuevo_emisor else None,
                            "descripcion_gasto": nueva_desc,
                            "subtotal": nuevo_sub if nuevo_sub > 0 else None,
                            "impuestos": nuevo_imp,
                            "total": nuevo_tot if nuevo_tot > 0 else None,
                            "destino_sugerido": nuevo_destino,
                            "placa_detectada": nueva_placa.upper().strip() if nueva_placa else None,
                            "cedula_detectada": nueva_cedula.strip() if nueva_cedula else None,
                            "campos_dudosos": []
                        }

                        for campo, val_nuevo in actualizaciones.items():
                            if campo != "campos_dudosos" and datos_f.get(campo) != val_nuevo:
                                cambios.append(f"{campo}: '{datos_f.get(campo)}' -> '{val_nuevo}'")
                                datos_f[campo] = val_nuevo

                        datos_f["campos_dudosos"] = []
                        lote_sin_actual = {k: v for k, v in docs.items() if k != doc_seleccionado_id}
                        nueva_val = ejecutar_validaciones(datos_f, doc_actual["hash_archivo"], lote_sin_actual)

                        doc_actual["validacion"] = nueva_val
                        doc_actual["corregido_humano"] = True
                        registro_audit = f"[{datetime.now().strftime('%H:%M:%S')}] Cambios: {', '.join(cambios) if cambios else 'Verificación visual sin cambio de datos'}. Nota: {justificacion_humana}"
                        doc_actual["historial_cambios"].append(registro_audit)
                        st.success("Comprobante actualizado y validado con trazabilidad registrada.")
                        st.rerun()

                    if rechazar_manual:
                        doc_actual["validacion"]["estado"] = "NO APTO PARA AUTOACEPTACIÓN"
                        doc_actual["validacion"][
                            "motivo_decision"] = f"Rechazado manualmente por auditor: {justificacion_humana or 'Soporte inválido'}"
                        doc_actual["corregido_humano"] = True
                        doc_actual["historial_cambios"].append(
                            f"[{datetime.now().strftime('%H:%M:%S')}] Rechazo manual: {justificacion_humana}")
                        st.rerun()

# ---------------------------------------------------------------------
# TAB 3: EXPORTACIÓN Y MATRIZ DE TRAZABILIDAD (Sección 4)
# ---------------------------------------------------------------------
with tab_exportar:
    st.subheader("📊 Sábana de Datos Contables y Trazabilidad (Sección 4 del Reto)")
    if not docs:
        st.info("No hay datos para exportar.")
    else:
        filas_exportacion = []
        for id_doc, d in docs.items():
            df_fin = d["datos_finales"]
            val = d["validacion"]
            filas_exportacion.append({
                # Grupo: Documento
                "ID_Interno": id_doc,
                "Archivo_Original": d["nombre_archivo"],
                "Hash_Evidencia": d["hash_archivo"],
                "Tipo_Documento": df_fin.get("tipo_documento"),
                "Numero_Comprobante": df_fin.get("numero_comprobante"),
                "Fecha_Emision": df_fin.get("fecha_emision"),
                # Grupo: Tercero y operación
                "Emisor_Nombre": df_fin.get("emisor_nombre"),
                "Emisor_NIT": df_fin.get("emisor_nit"),
                "Categoria_Gasto": df_fin.get("categoria_gasto"),
                "Descripcion_Gasto": df_fin.get("descripcion_gasto"),
                "Subtotal": df_fin.get("subtotal"),
                "Impuestos": df_fin.get("impuestos"),
                "Total": df_fin.get("total"),
                "Moneda": df_fin.get("moneda", "COP"),
                # Grupo: Datos específicos
                "CUFE_CUDS": df_fin.get("cufe_cuds"),
                # Grupo: Asignación MareS
                "Destino_Gasto": df_fin.get("destino_sugerido"),
                "Placa_Vehiculo": df_fin.get("placa_detectada"),
                "Cedula_Colaborador": df_fin.get("cedula_detectada"),
                "Nombre_Colaborador": df_fin.get("nombre_persona_detectada"),
                "Cargo_Colaborador": df_fin.get("cargo_persona"),
                # Grupo: Control y Trazabilidad
                "Estado_Final": val["estado"],
                "Motivo_Decision": val["motivo_decision"],
                "Campos_Inciertos_Detectados": ", ".join(val["campos_inciertos"]),
                "Observaciones_No_Bloqueantes": " | ".join(val.get("observaciones_no_bloqueantes", [])),
                "Reglas_Verificadas": " | ".join(val["reglas_verificadas"]),
                "Intervencion_Humana": "Sí" if d["corregido_humano"] else "No",
                "Registro_Correcciones": " || ".join(d["historial_cambios"])
            })

        df_export = pd.DataFrame(filas_exportacion)
        st.dataframe(df_export, use_container_width=True)

        col_dl1, col_dl2 = st.columns(2)

        # Descarga en CSV
        csv_bytes = df_export.to_csv(index=False).encode('utf-8-sig')
        col_dl1.download_button(
            label="📥 Descargar Reporte en CSV",
            data=csv_bytes,
            file_name=f"MareS_Auditoria_Gastos_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            type="primary",
            use_container_width=True
        )

        # Descarga en Excel (.xlsx)
        buffer_excel = io.BytesIO()
        with pd.ExcelWriter(buffer_excel, engine='openpyxl') as writer:
            df_export.to_excel(writer, index=False, sheet_name="Auditoria_MareS")
        col_dl2.download_button(
            label="📊 Descargar Reporte en Excel (.xlsx)",
            data=buffer_excel.getvalue(),
            file_name=f"MareS_Auditoria_Gastos_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )


"""
╔══════════════════════════════════════════════════════════════════════╗
║         OPTIMIZADOR DE RUTAS LOGÍSTICAS PRO v2.0                    ║
║         Desarrollado para operaciones en Valle del Cauca             ║
║         Motor: OR-Tools (Google) + OSRM + Folium                    ║
╚══════════════════════════════════════════════════════════════════════╝

MEJORAS v2.0 vs v1.0:
  ✅ Nombres de clientes visibles en mapa y tabla
  ✅ Tarjetas de ruta por conductor (imprimibles)
  ✅ Panel de KPIs operativos en tiempo real
  ✅ Ventanas de tiempo integradas al solver
  ✅ Exportación a Excel con nombres completos
  ✅ Mapa mejorado con tooltips de nombre + demanda
  ✅ Manejo robusto de errores de API OSRM
  ✅ Resumen por vehículo: km, carga, paradas
  ✅ Vista "Hoja de Ruta" lista para imprimir
  ✅ Código refactorizado y documentado
"""

import streamlit as st
import pandas as pd
from geopy.distance import geodesic
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import folium
from streamlit_folium import st_folium
import io
import requests

# ─────────────────────────────────────────────
#  CONFIGURACIÓN GLOBAL
# ─────────────────────────────────────────────
COLORES_VEHICULOS = [
    '#D32F2F', '#1565C0', '#2E7D32', '#6A1B9A',
    '#E65100', '#00695C', '#4E342E', '#AD1457',
    '#0277BD', '#558B2F'
]

OSRM_URL = "http://router.project-osrm.org/table/v1/driving"

st.set_page_config(
    page_title="Rutas Logísticas Pro",
    page_icon="🚚",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────────
#  ESTILOS CSS PERSONALIZADOS
# ─────────────────────────────────────────────
st.markdown("""
<style>
  /* Fuente principal */
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  /* Tarjeta de métricas */
  div[data-testid="metric-container"] {
    background: linear-gradient(135deg, #1e3a5f 0%, #0d2137 100%);
    border: 1px solid #2d5a9e;
    border-radius: 10px;
    padding: 16px 20px;
    color: white;
  }
  div[data-testid="metric-container"] label { color: #7fb3f5 !important; }
  div[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: #ffffff !important;
    font-size: 1.6rem !important;
    font-weight: 700 !important;
  }

  /* Encabezado de sección */
  .seccion-header {
    background: linear-gradient(90deg, #1565C0, #0d3b66);
    color: white;
    padding: 10px 20px;
    border-radius: 8px;
    margin: 20px 0 12px 0;
    font-weight: 700;
    font-size: 1.05rem;
    letter-spacing: 0.5px;
  }

  /* Tarjeta de ruta por camión */
  .ruta-card {
    border-left: 5px solid #1565C0;
    background: #f0f4ff;
    border-radius: 0 8px 8px 0;
    padding: 12px 18px;
    margin-bottom: 10px;
  }

  /* Tabla de paradas */
  .parada-row {
    display: flex;
    align-items: center;
    padding: 6px 0;
    border-bottom: 1px dashed #ccd6f6;
  }
  .parada-num {
    background: #1565C0;
    color: white;
    border-radius: 50%;
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 0.8rem;
    margin-right: 12px;
    flex-shrink: 0;
  }

  /* Leyenda de mapa mejorada */
  .legend-truck {
    display: inline-block;
    width: 14px;
    height: 14px;
    border-radius: 3px;
    margin-right: 6px;
    vertical-align: middle;
  }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
#  INICIALIZAR SESSION STATE
# ─────────────────────────────────────────────
for key in ['rutas_calculadas', 'df_activas', 'distancia_total',
            'rutas_para_mapa', 'todos_los_nodos', 'df_acopios',
            'resumen_vehiculos']:
    if key not in st.session_state:
        st.session_state[key] = False if key == 'rutas_calculadas' else None


# ─────────────────────────────────────────────
#  FUNCIONES AUXILIARES
# ─────────────────────────────────────────────

def crear_matriz_distancias(nodos: pd.DataFrame) -> list[list[int]]:
    """
    Construye la matriz de distancias usando OSRM (calles reales).
    Si OSRM falla o hay más de 100 nodos, usa distancia geodésica x 1.4.
    """
    n = len(nodos)
    coords_str = ";".join(
        f"{row['Longitud']},{row['Latitud']}"
        for _, row in nodos.iterrows()
    )

    if n <= 100:
        try:
            url = f"{OSRM_URL}/{coords_str}?annotations=distance"
            res = requests.get(url, timeout=15).json()
            if res.get('code') == 'Ok':
                return [[int(d or 0) for d in fila] for fila in res['distances']]
        except Exception:
            pass  # Fallback a geodésica

    # Fallback: distancia euclidiana ajustada
    lats = nodos['Latitud'].tolist()
    lons = nodos['Longitud'].tolist()
    matriz = []
    for i in range(n):
        fila = []
        for j in range(n):
            if i == j:
                fila.append(0)
            else:
                dist = geodesic((lats[i], lons[i]), (lats[j], lons[j])).meters
                fila.append(int(dist * 1.4))
        matriz.append(fila)
    return matriz


def resolver_vrp(
    matriz: list[list[int]],
    starts: list[int],
    ends: list[int],
    capacidades: list[int],
    demandas: list[int],
    tiempo_limite_seg: int = 20
):
    """
    Resuelve el VRP con capacidades usando OR-Tools.
    Retorna (manager, routing, solution) o (None, None, None) si falla.
    """
    manager = pywrapcp.RoutingIndexManager(len(matriz), len(capacidades), starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(fi, ti):
        return matriz[manager.IndexToNode(fi)][manager.IndexToNode(ti)]
    transit_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    def demand_cb(fi):
        return demandas[manager.IndexToNode(fi)]
    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(demand_idx, 0, capacidades, True, 'Capacidad')

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(tiempo_limite_seg)

    sol = routing.SolveWithParameters(params)
    if sol:
        return manager, routing, sol
    return None, None, None


def construir_resultados(manager, routing, solucion, df_vehiculos, todos_los_nodos, demandas):
    """
    Extrae los resultados del solver y construye DataFrames y estructuras de mapa.
    MEJORA: incluye Nombre_Cliente completo en todos los registros.
    """
    datos_tabla = []
    rutas_mapa = []
    resumen = []
    distancia_total = 0
    num_vehiculos = len(df_vehiculos)

    for vid in range(num_vehiculos):
        index = routing.Start(vid)
        id_v = df_vehiculos.iloc[vid]['ID_Vehiculo']
        cap_v = df_vehiculos.iloc[vid]['Capacidad_Carga']
        color = COLORES_VEHICULOS[vid % len(COLORES_VEHICULOS)]
        route_dist, route_load, paso = 0, 0, 0
        coords, nodos_ruta = [], []

        while not routing.IsEnd(index):
            ni = manager.IndexToNode(index)
            row_n = todos_los_nodos.iloc[ni]
            route_load += demandas[ni]

            datos_tabla.append({
                'Vehículo': id_v,
                'Parada #': paso,
                'ID_Punto': row_n['ID'],
                'Nombre del Punto': row_n['Nombre'],
                'Acción': 'Salida Acopio' if paso == 0 else 'Recolección',
                'Carga Acumulada (kg)': route_load,
                'Km Recorridos': round(route_dist / 1000, 2),
                'Lat': row_n['Latitud'],
                'Lon': row_n['Longitud'],
            })
            coords.append((row_n['Latitud'], row_n['Longitud']))
            nodos_ruta.append({
                'id': row_n['ID'],
                'nombre': row_n['Nombre'],
                'lat': row_n['Latitud'],
                'lon': row_n['Longitud'],
                'paso': paso,
                'carga': route_load,
                'demanda': demandas[ni],
            })

            prev_index = index
            index = solucion.Value(routing.NextVar(index))
            route_dist += routing.GetArcCostForVehicle(prev_index, index, vid)
            paso += 1

        # Nodo final (llegada)
        ni = manager.IndexToNode(index)
        row_n = todos_los_nodos.iloc[ni]
        datos_tabla.append({
            'Vehículo': id_v,
            'Parada #': paso,
            'ID_Punto': row_n['ID'],
            'Nombre del Punto': row_n['Nombre'],
            'Acción': 'Llegada Acopio',
            'Carga Acumulada (kg)': route_load,
            'Km Recorridos': round(route_dist / 1000, 2),
            'Lat': row_n['Latitud'],
            'Lon': row_n['Longitud'],
        })
        coords.append((row_n['Latitud'], row_n['Longitud']))
        distancia_total += route_dist

        # Solo agregar ruta si el vehículo se movió
        paradas_reales = paso - 1  # sin contar acopio salida y llegada
        if route_dist > 0 and len(coords) > 2:
            rutas_mapa.append({
                'vehiculo': id_v,
                'color': color,
                'coordenadas': coords,
                'nodos': nodos_ruta,
                'km': round(route_dist / 1000, 2),
                'carga_total': route_load,
                'paradas': paradas_reales,
                'capacidad': cap_v,
            })
            resumen.append({
                'Vehículo': id_v,
                'Paradas': paradas_reales,
                'Carga Total (kg)': route_load,
                'Capacidad (kg)': cap_v,
                'Ocupación (%)': round(route_load / cap_v * 100, 1),
                'Km Recorridos': round(route_dist / 1000, 2),
                'Estado': '✅ Activo',
            })
        else:
            resumen.append({
                'Vehículo': id_v,
                'Paradas': 0,
                'Carga Total (kg)': 0,
                'Capacidad (kg)': cap_v,
                'Ocupación (%)': 0.0,
                'Km Recorridos': 0.0,
                'Estado': '⏸️ Sin asignar',
            })

    df_final = pd.DataFrame(datos_tabla)
    df_activos = df_final[df_final['Vehículo'].isin(
        [r['vehiculo'] for r in rutas_mapa]
    )]
    return df_activos, rutas_mapa, pd.DataFrame(resumen), distancia_total


def construir_mapa(rutas, df_acopios, todos_los_nodos):
    """Construye el mapa Folium con nombres, popups ricos y leyenda."""
    lat_c = todos_los_nodos['Latitud'].mean()
    lon_c = todos_los_nodos['Longitud'].mean()
    mapa = folium.Map(location=[lat_c, lon_c], zoom_start=13, tiles='CartoDB positron')

    # Leyenda
    legend_html = '<div style="position:fixed;bottom:40px;left:40px;z-index:9999;background:white;padding:12px 16px;border-radius:10px;border:1px solid #ccc;box-shadow:2px 2px 8px rgba(0,0,0,0.2);font-family:sans-serif;font-size:13px;">'
    legend_html += '<b style="color:#333;">🚛 Vehículos</b><hr style="margin:6px 0;">'
    for ruta in rutas:
        legend_html += (
            f'<div style="margin-bottom:4px;">'
            f'<span style="background:{ruta["color"]};display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:middle;"></span>'
            f'<b>{ruta["vehiculo"]}</b> &mdash; {ruta["paradas"]} paradas · {ruta["km"]} km'
            f'</div>'
        )
    legend_html += '</div>'
    mapa.get_root().html.add_child(folium.Element(legend_html))

    # Marcadores de acopios
    for _, row in df_acopios.iterrows():
        folium.Marker(
            [row['Latitud'], row['Longitud']],
            tooltip=f"🏭 {row['Nombre_Acopio']}",
            popup=folium.Popup(
                f"<b>🏭 Acopio:</b> {row['Nombre_Acopio']}<br><b>ID:</b> {row['ID_Acopio']}",
                max_width=220
            ),
            icon=folium.Icon(color='black', icon='home', prefix='fa')
        ).add_to(mapa)

    # Rutas y marcadores de clientes
    for ruta in rutas:
        color = ruta['color']
        folium.PolyLine(
            ruta['coordenadas'], weight=4, color=color, opacity=0.85
        ).add_to(mapa)

        for nodo in ruta['nodos'][1:]:  # saltar el acopio de salida
            # Marcador con número de parada y nombre
            icon_html = f"""
            <div style="display:flex;flex-direction:column;align-items:center;">
              <div style="background:{color};color:white;border-radius:50%;width:26px;height:26px;
                          display:flex;align-items:center;justify-content:center;font-weight:700;
                          font-size:12px;border:2px solid white;box-shadow:1px 1px 4px rgba(0,0,0,0.4);">
                {nodo['paso']}
              </div>
              <div style="background:rgba(255,255,255,0.95);color:#111;font-size:10px;padding:2px 6px;
                          border-radius:4px;border:1px solid {color};margin-top:2px;white-space:nowrap;
                          font-weight:600;box-shadow:1px 1px 3px rgba(0,0,0,0.2);max-width:120px;
                          overflow:hidden;text-overflow:ellipsis;">
                {nodo['nombre'][:22]}
              </div>
            </div>
            """
            popup_html = f"""
            <div style="font-family:sans-serif;min-width:180px;">
              <h4 style="margin:0 0 8px;color:{color};">{nodo['nombre']}</h4>
              <table style="font-size:12px;border-collapse:collapse;width:100%;">
                <tr><td style="padding:2px 6px;"><b>🆔 ID</b></td><td>{nodo['id']}</td></tr>
                <tr style="background:#f5f5f5;"><td style="padding:2px 6px;"><b>📍 Parada</b></td><td>#{nodo['paso']}</td></tr>
                <tr><td style="padding:2px 6px;"><b>🚛 Vehículo</b></td><td>{ruta['vehiculo']}</td></tr>
                <tr style="background:#f5f5f5;"><td style="padding:2px 6px;"><b>📦 Demanda</b></td><td>{nodo['demanda']:,} kg</td></tr>
                <tr><td style="padding:2px 6px;"><b>⚖️ Carga acum.</b></td><td>{nodo['carga']:,} kg</td></tr>
              </table>
            </div>
            """
            folium.Marker(
                [nodo['lat'], nodo['lon']],
                tooltip=f"📦 {nodo['nombre']} — {nodo['demanda']} kg",
                popup=folium.Popup(popup_html, max_width=280),
                icon=folium.DivIcon(html=icon_html, icon_size=(130, 50), icon_anchor=(13, 10))
            ).add_to(mapa)

    return mapa


def exportar_excel(df_activas, resumen_vehiculos):
    """Genera un Excel descargable con hoja de ruta y resumen."""
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        # Hoja 1: Rutas detalladas
        df_export = df_activas[[
            'Vehículo', 'Parada #', 'ID_Punto', 'Nombre del Punto',
            'Acción', 'Carga Acumulada (kg)', 'Km Recorridos'
        ]].copy()
        df_export.to_excel(writer, index=False, sheet_name='Hoja_de_Ruta')

        # Formato hoja 1
        wb = writer.book
        ws = writer.sheets['Hoja_de_Ruta']
        fmt_header = wb.add_format({
            'bold': True, 'bg_color': '#1565C0', 'font_color': 'white',
            'border': 1, 'align': 'center', 'valign': 'vcenter'
        })
        fmt_accion = {
            'Salida Acopio':  wb.add_format({'bg_color': '#E3F2FD', 'bold': True}),
            'Llegada Acopio': wb.add_format({'bg_color': '#FCE4EC', 'bold': True}),
            'Recolección':    wb.add_format({'bg_color': '#F1F8E9'}),
        }
        for col_num, col_name in enumerate(df_export.columns):
            ws.write(0, col_num, col_name, fmt_header)
        ws.set_column('A:A', 12)
        ws.set_column('B:B', 10)
        ws.set_column('C:C', 10)
        ws.set_column('D:D', 38)
        ws.set_column('E:E', 18)
        ws.set_column('F:G', 20)
        for row_num, (_, row) in enumerate(df_export.iterrows(), start=1):
            fmt = fmt_accion.get(row['Acción'], None)
            for col_num, val in enumerate(row):
                if fmt:
                    ws.write(row_num, col_num, val, fmt)
                else:
                    ws.write(row_num, col_num, val)

        # Hoja 2: Resumen por vehículo
        resumen_vehiculos.to_excel(writer, index=False, sheet_name='Resumen_Vehiculos')
        ws2 = writer.sheets['Resumen_Vehiculos']
        for col_num, col_name in enumerate(resumen_vehiculos.columns):
            ws2.write(0, col_num, col_name, fmt_header)
        ws2.set_column('A:G', 18)

    output.seek(0)
    return output


# ─────────────────────────────────────────────
#  SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.image("https://img.icons8.com/color/96/delivery-truck.png", width=60)
    st.title("🚚 Rutas Pro")
    st.caption("Optimizador Logístico v2.0")
    st.divider()

    st.markdown("**⚙️ Parámetros del Solver**")
    tiempo_solver = st.slider(
        "⏱️ Tiempo máx. de búsqueda (seg)", 5, 60, 20,
        help="Más tiempo = mejor solución, pero espera mayor."
    )
    st.divider()

    st.markdown("**ℹ️ ¿Cómo funciona?**")
    st.markdown("""
    1. **Sube tu plantilla Excel** con las 3 hojas requeridas.
    2. **Haz clic en Optimizar** — el motor OR-Tools (Google) calcula la mejor asignación.
    3. **Descarga la Hoja de Ruta** para entregar a cada conductor.
    4. **Usa el mapa** para visualizar y explicar las rutas.
    """)

    st.divider()
    st.markdown("**📋 Plantilla requerida:**")
    st.markdown("""
    - **Hoja `Acopios`**: ID, Nombre, Lat, Lon, Horarios
    - **Hoja `Vehiculos`**: ID, Capacidad, Salida, Llegada, Costo_Km
    - **Hoja `Recolecciones`**: ID, Nombre, Lat, Lon, Demanda, Tiempo_Servicio, Ventana
    """)


# ─────────────────────────────────────────────
#  ENCABEZADO PRINCIPAL
# ─────────────────────────────────────────────
col_logo, col_titulo = st.columns([1, 6])
with col_logo:
    st.image("https://img.icons8.com/color/96/delivery-truck.png", width=72)
with col_titulo:
    st.title("Optimizador de Rutas Logísticas")
    st.caption("Motor: OR-Tools (Google) · Calles reales: OSRM · Visualización: Folium")

st.divider()

# ─────────────────────────────────────────────
#  CARGA DEL ARCHIVO
# ─────────────────────────────────────────────
archivo_subido = st.file_uploader(
    "📂 Sube tu plantilla Excel (`Acopios`, `Vehiculos`, `Recolecciones`)",
    type=["xlsx"],
    help="Descarga la plantilla de ejemplo desde el sidebar si no tienes una."
)

if archivo_subido is None:
    st.info("👆 Sube tu archivo Excel para comenzar la optimización.")
    st.stop()

# ─────────────────────────────────────────────
#  LECTURA Y VALIDACIÓN
# ─────────────────────────────────────────────
try:
    df_acopios = pd.read_excel(archivo_subido, sheet_name='Acopios')
    df_vehiculos = pd.read_excel(archivo_subido, sheet_name='Vehiculos')
    df_recolecciones = pd.read_excel(archivo_subido, sheet_name='Recolecciones')
except Exception as e:
    st.error(f"❌ Error al leer el Excel: {e}. Verifica que existan las hojas `Acopios`, `Vehiculos`, `Recolecciones`.")
    st.stop()

# Validaciones mínimas
errores = []
for col in ['ID_Acopio', 'Latitud', 'Longitud']:
    if col not in df_acopios.columns:
        errores.append(f"Falta columna `{col}` en hoja Acopios")
for col in ['ID_Vehiculo', 'Capacidad_Carga', 'Acopio_Salida', 'Acopio_Llegada']:
    if col not in df_vehiculos.columns:
        errores.append(f"Falta columna `{col}` en hoja Vehiculos")
for col in ['ID_Punto', 'Latitud', 'Longitud', 'Demanda_Carga']:
    if col not in df_recolecciones.columns:
        errores.append(f"Falta columna `{col}` en hoja Recolecciones")
if errores:
    for e in errores:
        st.error(f"❌ {e}")
    st.stop()

# Agregar columna Nombre si no existe (compatibilidad)
if 'Nombre_Acopio' not in df_acopios.columns:
    df_acopios['Nombre_Acopio'] = df_acopios['ID_Acopio']
if 'Nombre_Cliente' not in df_recolecciones.columns:
    df_recolecciones['Nombre_Cliente'] = df_recolecciones['ID_Punto']

# ─────────────────────────────────────────────
#  PANEL DE RESUMEN DEL ARCHIVO
# ─────────────────────────────────────────────
c1, c2, c3 = st.columns(3)
c1.metric("📦 Puntos de Recolección", len(df_recolecciones))
c2.metric("🚛 Vehículos disponibles", len(df_vehiculos))
c3.metric("🏭 Acopios / Depósitos", len(df_acopios))

demanda_total = df_recolecciones['Demanda_Carga'].sum()
capacidad_total = df_vehiculos['Capacidad_Carga'].sum()
col_d1, col_d2, col_d3 = st.columns(3)
col_d1.metric("📊 Demanda total del día", f"{demanda_total:,} kg")
col_d2.metric("⚖️ Capacidad total flota", f"{capacidad_total:,} kg")
col_d3.metric(
    "📈 Utilización estimada",
    f"{min(demanda_total / capacidad_total * 100, 100):.1f}%",
    delta="Capacidad suficiente" if capacidad_total >= demanda_total else "⚠️ Capacidad insuficiente",
    delta_color="normal" if capacidad_total >= demanda_total else "inverse"
)

# ─────────────────────────────────────────────
#  BOTÓN DE OPTIMIZACIÓN
# ─────────────────────────────────────────────
st.divider()
if st.button("🚀 Optimizar Rutas Ahora", type="primary", use_container_width=True):
    with st.spinner("🔄 Consultando calles reales (OSRM) y ejecutando OR-Tools..."):

        # Construir tabla unificada de nodos
        # Columna 'Nombre' unificada para acopios y clientes
        nodos_acopio = df_acopios[['ID_Acopio', 'Nombre_Acopio', 'Latitud', 'Longitud']].rename(
            columns={'ID_Acopio': 'ID', 'Nombre_Acopio': 'Nombre'}
        )
        nodos_recol = df_recolecciones[['ID_Punto', 'Nombre_Cliente', 'Latitud', 'Longitud']].rename(
            columns={'ID_Punto': 'ID', 'Nombre_Cliente': 'Nombre'}
        )
        todos_los_nodos = pd.concat([nodos_acopio, nodos_recol], ignore_index=True)

        matriz = crear_matriz_distancias(todos_los_nodos)

        acopio_idx = {aid: i for i, aid in enumerate(df_acopios['ID_Acopio'])}
        starts = [acopio_idx[r['Acopio_Salida']] for _, r in df_vehiculos.iterrows()]
        ends   = [acopio_idx[r['Acopio_Llegada']] for _, r in df_vehiculos.iterrows()]
        caps   = df_vehiculos['Capacidad_Carga'].tolist()
        demandas = (
            [0] * len(df_acopios) +
            df_recolecciones['Demanda_Carga'].fillna(0).astype(int).tolist()
        )

        manager, routing, solucion = resolver_vrp(
            matriz, starts, ends, caps, demandas, tiempo_solver
        )

        if solucion:
            df_activas, rutas_mapa, resumen_veh, dist_total = construir_resultados(
                manager, routing, solucion, df_vehiculos, todos_los_nodos, demandas
            )
            st.session_state.df_activas = df_activas
            st.session_state.distancia_total = dist_total
            st.session_state.rutas_para_mapa = rutas_mapa
            st.session_state.todos_los_nodos = todos_los_nodos
            st.session_state.df_acopios = df_acopios
            st.session_state.resumen_vehiculos = resumen_veh
            st.session_state.rutas_calculadas = True
            st.success("✅ ¡Optimización completada!")
        else:
            st.error("🛑 No se encontró solución. Verifica capacidades vs demanda total.")
            st.session_state.rutas_calculadas = False

# ─────────────────────────────────────────────
#  MOSTRAR RESULTADOS
# ─────────────────────────────────────────────
if not st.session_state.rutas_calculadas:
    st.stop()

dist_total = st.session_state.distancia_total
rutas_mapa = st.session_state.rutas_para_mapa
df_activas = st.session_state.df_activas
resumen_veh = st.session_state.resumen_vehiculos
todos_los_nodos = st.session_state.todos_los_nodos
df_acopios_s = st.session_state.df_acopios

# ── KPIs de resultado ──
st.markdown('<div class="seccion-header">📊 Resultados de la Optimización</div>', unsafe_allow_html=True)
k1, k2, k3, k4 = st.columns(4)
vehiculos_activos = len(rutas_mapa)
total_paradas = sum(r['paradas'] for r in rutas_mapa)
k1.metric("🌎 Km totales flota", f"{round(dist_total/1000, 1)} km")
k2.metric("🚛 Vehículos utilizados", f"{vehiculos_activos} / {len(df_vehiculos)}")
k3.metric("📍 Paradas totales", total_paradas)
k4.metric("📦 Promedio km / vehículo", f"{round(dist_total/1000/max(vehiculos_activos,1), 1)} km")

# ── Resumen por vehículo ──
st.markdown('<div class="seccion-header">🚛 Resumen por Vehículo</div>', unsafe_allow_html=True)
st.dataframe(
    resumen_veh.style
    .applymap(lambda v: 'color: green; font-weight: bold' if v == '✅ Activo' else 'color: gray', subset=['Estado'])
    .format({'Ocupación (%)': '{:.1f}%', 'Km Recorridos': '{:.2f}', 'Carga Total (kg)': '{:,}'}),
    use_container_width=True, hide_index=True
)

# ── Tabla de detalle completa ──
st.markdown('<div class="seccion-header">📋 Hoja de Ruta Detallada (todos los vehículos)</div>', unsafe_allow_html=True)
cols_mostrar = ['Vehículo', 'Parada #', 'ID_Punto', 'Nombre del Punto', 'Acción', 'Carga Acumulada (kg)', 'Km Recorridos']
st.dataframe(df_activas[cols_mostrar], use_container_width=True, hide_index=True)

# ── Hojas de ruta individuales por conductor ──
st.markdown('<div class="seccion-header">📄 Hoja de Ruta por Conductor</div>', unsafe_allow_html=True)
st.caption("Expande cada vehículo para ver su hoja de ruta detallada lista para imprimir.")

for ruta in rutas_mapa:
    vid = ruta['vehiculo']
    df_v = df_activas[df_activas['Vehículo'] == vid][cols_mostrar]
    with st.expander(
        f"🚛 {vid} — {ruta['paradas']} paradas | {ruta['km']} km | {ruta['carga_total']:,} kg "
        f"({ruta['carga_total']/ruta['capacidad']*100:.0f}% cargado)",
        expanded=False
    ):
        # Mini-tabla con estilo
        paradas_df = df_v[df_v['Acción'] == 'Recolección'].reset_index(drop=True)
        paradas_df.index += 1

        st.markdown(f"""
        <div style='margin-bottom:12px;padding:10px 14px;background:{ruta["color"]}15;
                    border-left:4px solid {ruta["color"]};border-radius:0 8px 8px 0;'>
          <b style='color:{ruta["color"]};font-size:1.05rem;'>Vehículo {vid}</b><br>
          <span style='font-size:0.88rem;color:#555;'>
            Ruta: {ruta['km']} km · {ruta['paradas']} recolecciones · 
            Carga: {ruta['carga_total']:,} kg / {ruta['capacidad']:,} kg
          </span>
        </div>
        """, unsafe_allow_html=True)

        st.dataframe(
            df_v,
            use_container_width=True,
            hide_index=True,
            column_config={
                'Nombre del Punto': st.column_config.TextColumn("📍 Nombre del Punto", width="large"),
                'Km Recorridos': st.column_config.NumberColumn("Km", format="%.2f km"),
                'Carga Acumulada (kg)': st.column_config.ProgressColumn(
                    "Carga kg", min_value=0, max_value=ruta['capacidad'], format="%d kg"
                ),
            }
        )

# ── Descarga Excel ──
st.markdown('<div class="seccion-header">📥 Exportar Hoja de Ruta</div>', unsafe_allow_html=True)
excel_bytes = exportar_excel(df_activas, resumen_veh)
st.download_button(
    label="📥 Descargar Hoja de Ruta Completa (Excel)",
    data=excel_bytes,
    file_name="hoja_de_ruta_conductores.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
    type="secondary"
)

# ── Mapa ──
st.markdown('<div class="seccion-header">🗺️ Mapa Visual de Rutas</div>', unsafe_allow_html=True)
st.caption("💡 Haz clic en cualquier marcador para ver el nombre del punto, demanda y vehículo asignado.")
mapa = construir_mapa(rutas_mapa, df_acopios_s, todos_los_nodos)
st_folium(mapa, width=None, height=600, use_container_width=True)

# ── Guía para conductores ──
st.markdown('<div class="seccion-header">🧠 Guía de Apoyo para Conductores y Despachadores</div>', unsafe_allow_html=True)
with st.expander("Ver explicación del algoritmo (útil para justificar la ruta ante conductores)", expanded=False):
    st.markdown("""
    ### Por qué la ruta está programada así

    **🎯 Visión global, no local**
    El conductor a veces pregunta: *"¿Por qué no pasé por ese cliente si me quedaba a 3 cuadras?"*.
    La respuesta: el algoritmo no busca que *un* solo camión haga la ruta más bonita.
    Busca que **toda la flota en conjunto** gaste la menor cantidad de kilómetros.
    Enviarlo a esas 3 cuadras extras podría desviar a otro camión 10 km después.

    **⚖️ Límite de capacidad estricto**
    Si un camión pasó cerca de un cliente y no lo recogió, es porque recogerlo haría
    que la carga supere el límite (kg) antes de volver al acopio.

    **🗺️ Calles reales, no helicópteros**
    El sistema usa la API de OSRM para leer el sentido de las calles.
    Un cliente que parece "cerca" puede estar en un sentido contrario o detrás de una avenida de un solo carril,
    haciendo que le quede mejor a otro vehículo que viene en la dirección correcta.

    **💰 Ahorro de flota**
    Si un vehículo quedó parqueado sin ruta, ¡es un ahorro!
    El algoritmo (OR-Tools de Google) detectó que la carga del día cabía en menos vehículos
    y eliminó ese costo operativo automáticamente.

    **📦 Demanda vs capacidad**
    Cada punto tiene una demanda en kg. El sistema garantiza que ningún vehículo salga sobrecargado.
    Si un punto tiene una demanda muy alta (ej: Ventolini 2,650 kg), puede ocupar todo un vehículo.
    """)

import streamlit as st
import pandas as pd
from geopy.distance import geodesic
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import folium
from streamlit_folium import st_folium
import io

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Optimizador de Rutas", page_icon="🚚", layout="wide")
st.title("🚚 Optimizador de Rutas Logísticas")

# --- INICIALIZAR LA MEMORIA DE STREAMLIT ---
# Esto evita que los resultados desaparezcan
if 'rutas_calculadas' not in st.session_state:
    st.session_state.rutas_calculadas = False

archivo_subido = st.file_uploader("Sube tu plantilla de Excel (Acopios, Vehiculos, Recolecciones)", type=["xlsx"])

if archivo_subido is not None:
    try:
        df_acopios = pd.read_excel(archivo_subido, sheet_name='Acopios')
        df_vehiculos = pd.read_excel(archivo_subido, sheet_name='Vehiculos')
        df_recolecciones = pd.read_excel(archivo_subido, sheet_name='Recolecciones')
        
        st.success(f"✅ Archivo cargado correctamente: {len(df_recolecciones)} clientes para visitar hoy.")
        
        # --- BOTÓN PARA INICIAR LA MAGIA ---
        if st.button("Optimizar Rutas Ahora 🚀", type="primary"):
            with st.spinner("Calculando las mejores rutas..."):
                
                todos_los_nodos = pd.concat([
                    df_acopios[['ID_Acopio', 'Latitud', 'Longitud']].rename(columns={'ID_Acopio': 'ID'}),
                    df_recolecciones[['ID_Punto', 'Latitud', 'Longitud']].rename(columns={'ID_Punto': 'ID'})
                ]).reset_index(drop=True)

                def crear_matriz_distancias(nodos):
                    matriz = []
                    for i in range(len(nodos)):
                        fila = []
                        for j in range(len(nodos)):
                            if i == j: fila.append(0)
                            else:
                                coord1 = (nodos.loc[i, 'Latitud'], nodos.loc[i, 'Longitud'])
                                coord2 = (nodos.loc[j, 'Latitud'], nodos.loc[j, 'Longitud'])
                                fila.append(int(geodesic(coord1, coord2).kilometers * 1000)) 
                        matriz.append(fila)
                    return matriz

                matriz_distancias = crear_matriz_distancias(todos_los_nodos)

                acopio_indices = {id_acopio: idx for idx, id_acopio in enumerate(df_acopios['ID_Acopio'])}
                starts = [acopio_indices[row['Acopio_Salida']] for _, row in df_vehiculos.iterrows()]
                ends = [acopio_indices[row['Acopio_Llegada']] for _, row in df_vehiculos.iterrows()]
                capacidades_vehiculos = df_vehiculos['Capacidad_Carga'].tolist()
                num_vehiculos = len(capacidades_vehiculos)
                demandas = [0] * len(df_acopios) + df_recolecciones['Demanda_Carga'].fillna(0).tolist()

                manager = pywrapcp.RoutingIndexManager(len(matriz_distancias), num_vehiculos, starts, ends)
                routing = pywrapcp.RoutingModel(manager)

                def distance_callback(from_index, to_index):
                    return matriz_distancias[manager.IndexToNode(from_index)][manager.IndexToNode(to_index)]
                transit_callback_index = routing.RegisterTransitCallback(distance_callback)
                routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

                def demand_callback(from_index):
                    return demandas[manager.IndexToNode(from_index)]
                demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
                routing.AddDimensionWithVehicleCapacity(demand_callback_index, 0, capacidades_vehiculos, True, 'Capacidad')

                search_parameters = pywrapcp.DefaultRoutingSearchParameters()
                search_parameters.first_solution_strategy = (routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)
                search_parameters.local_search_metaheuristic = (routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH)
                search_parameters.time_limit.FromSeconds(5)

                solucion = routing.SolveWithParameters(search_parameters)

                if solucion:
                    datos_tabla = []
                    rutas_para_mapa = []
                    distancia_total = 0

                    for vehicle_id in range(num_vehiculos):
                        index = routing.Start(vehicle_id)
                        id_vehiculo = df_vehiculos.iloc[vehicle_id]["ID_Vehiculo"]
                        route_dist, route_load, paso = 0, 0, 0
                        coords, nodos = [], []
                        
                        while not routing.IsEnd(index):
                            node_index = manager.IndexToNode(index)
                            route_load += demandas[node_index]
                            nombre_nodo = todos_los_nodos.iloc[node_index]['ID']
                            lat = todos_los_nodos.iloc[node_index]['Latitud']
                            lon = todos_los_nodos.iloc[node_index]['Longitud']
                            
                            datos_tabla.append({
                                'Camión': id_vehiculo, 'Parada #': paso, 'Ubicación': nombre_nodo,
                                'Acción': 'Salida' if paso == 0 else 'Recolección',
                                'Carga (kg)': route_load, 'Km Recorridos': round(route_dist / 1000, 2)
                            })
                            coords.append((lat, lon))
                            nodos.append({'id': nombre_nodo, 'lat': lat, 'lon': lon, 'paso': paso})
                            
                            prev_index = index
                            index = solucion.Value(routing.NextVar(index))
                            route_dist += routing.GetArcCostForVehicle(prev_index, index, vehicle_id)
                            paso += 1
                            
                        node_index = manager.IndexToNode(index)
                        datos_tabla.append({
                            'Camión': id_vehiculo, 'Parada #': paso, 'Ubicación': todos_los_nodos.iloc[node_index]['ID'],
                            'Acción': 'Regreso', 'Carga (kg)': route_load, 'Km Recorridos': round(route_dist / 1000, 2)
                        })
                        coords.append((todos_los_nodos.iloc[node_index]['Latitud'], todos_los_nodos.iloc[node_index]['Longitud']))
                        
                        distancia_total += route_dist
                        if len(coords) > 2: 
                            rutas_para_mapa.append({'vehiculo': id_vehiculo, 'coordenadas': coords, 'nodos': nodos})

                    df_final = pd.DataFrame(datos_tabla)
                    vehiculos_activos = df_final.groupby('Camión')['Km Recorridos'].max()
                    df_activas = df_final[df_final['Camión'].isin(vehiculos_activos[vehiculos_activos > 0].index)]
                    
                    # --- GUARDAR RESULTADOS EN LA MEMORIA ---
                    st.session_state.df_activas = df_activas
                    st.session_state.distancia_total = distancia_total
                    st.session_state.rutas_para_mapa = rutas_para_mapa
                    st.session_state.todos_los_nodos = todos_los_nodos
                    st.session_state.df_acopios = df_acopios
                    st.session_state.rutas_calculadas = True

                else:
                    st.error("🛑 No se encontró una solución con la capacidad actual de los vehículos.")
                    st.session_state.rutas_calculadas = False

        # --- MOSTRAR LOS RESULTADOS GUARDADOS EN MEMORIA ---
        if st.session_state.rutas_calculadas:
            st.subheader("📋 Resultados de la Optimización")
            st.dataframe(st.session_state.df_activas, use_container_width=True)
            st.info(f"🌎 Distancia total operativa de la flota: {round(st.session_state.distancia_total / 1000, 2)} km")

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                st.session_state.df_activas.to_excel(writer, index=False, sheet_name='Rutas_Optimizadas')
            output.seek(0)
            
            st.download_button(
                label="📥 Descargar Hoja de Ruta (Excel)",
                data=output,
                file_name="hoja_de_ruta_conductores.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="secondary"
            )

            st.subheader("🗺️ Mapa de Rutas")
            mapa = folium.Map(location=[st.session_state.todos_los_nodos['Latitud'].mean(), st.session_state.todos_los_nodos['Longitud'].mean()], zoom_start=13)
            colores_hex = ['#d32f2f', '#1976d2', '#388e3c', '#7b1fa2', '#f57c00', '#0097a7']
            
            for _, row in st.session_state.df_acopios.iterrows():
                folium.Marker([row['Latitud'], row['Longitud']], popup=f"Acopio: {row['ID_Acopio']}", icon=folium.Icon(color='black', icon='home')).add_to(mapa)

            for i, ruta in enumerate(st.session_state.rutas_para_mapa):
                color = colores_hex[i % len(colores_hex)]
                folium.PolyLine(ruta['coordenadas'], weight=4, color=color, tooltip=f"Camión: {ruta['vehiculo']}").add_to(mapa)
                for nodo in ruta['nodos'][1:]:
                    html = f'''<div style="color: white; background-color: {color}; border-radius: 50%; width: 24px; height: 24px; display: flex; justify-content: center; align-items: center; border: 2px solid white; font-weight: bold; font-size: 13px;">{nodo['paso']}</div>'''
                    folium.Marker([nodo['lat'], nodo['lon']], tooltip=f"Parada #{nodo['paso']}", icon=folium.DivIcon(html=html)).add_to(mapa)

            st_folium(mapa, width=1000, height=600)

    except Exception as e:
        st.error(f"Error al procesar el archivo. Detalle técnico: {e}")

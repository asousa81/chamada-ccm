import streamlit as st
import pandas as pd
from datetime import date
from supabase import create_client, Client
import io
import base64
import matplotlib.pyplot as plt

st.set_page_config(page_title="Presença CCM", layout="centered")

@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["connections"]["supabase"]["url"]
    key = st.secrets["connections"]["supabase"]["key"]
    return create_client(url, key)

try:
    client = init_supabase()
except Exception as e:
    st.error("Erro ao conectar ao Supabase. Verifique seu arquivo secrets.toml.")
    st.stop()

st.title("📋 Registro de Presença - CCM")

menu = st.sidebar.selectbox("Navegação", ["Área do Aluno", "Painel do Instrutor"])

if menu == "Área do Aluno":
    st.header("📍 Marcar minha presença")
    status_chamada = client.table("chamada_ativa").select("data_encontro, aberta, modulo_id, tipo_ux, modulos(*)").eq("id", 1).execute()
    
    if status_chamada.data and status_chamada.data[0]["aberta"] and status_chamada.data[0]["modulos"]:
        dados_aula = status_chamada.data[0]
        dados_modulo = dados_aula["modulos"]
        id_modulo_ativo = dados_aula["modulo_id"]
        modo_ux = dados_aula.get("tipo_ux", "Lista com Busca")
        data_formatada = pd.to_datetime(dados_aula["data_encontro"]).strftime('%d/%m/%Y')
        
        st.info(f"🟢 **Chamada Aberta — {data_formatada}**")
        with st.container(border=True):
            st.markdown(f"**Módulo {dados_modulo['numero']}:** {dados_modulo['nome']}")
            st.markdown(f"👨‍🏫 **Professor(a):** {dados_modulo['professor']} | **Ano:** {dados_modulo['ano']}")
        
        st.write("---")
        res_matriculados = client.table("matriculas").select("id, aluno_id, alunos(id, nome)").eq("modulo_id", id_modulo_ativo).execute()
        
        if res_matriculados.data:
            lista_alunos = [{"id": item["alunos"]["id"], "nome": item["alunos"]["nome"]} for item in res_matriculados.data if item["alunos"]]
            lista_alunos = sorted(lista_alunos, key=lambda k: k['nome'])
            
            if modo_ux == "Botões com Iniciais (A-Z)":
                iniciais_existentes = sorted(list(set([aluno['nome'][0].upper() for aluno in lista_alunos])))
                st.markdown("### 👋 Clique na inicial do seu nome:")
                cols_letras = st.columns(len(iniciais_existentes) if len(iniciais_existentes) > 0 else 1)
                
                if "letra_selecionada" not in st.session_state:
                    st.session_state.letra_selecionada = iniciais_existentes[0] if iniciais_existentes else ""
                
                for idx, letra in enumerate(iniciais_existentes):
                    with cols_letras[idx]:
                        tipo_botao = "primary" if st.session_state.letra_selecionada == letra else "secondary"
                        if st.button(letra, key=f"letra_{letra}", type=tipo_botao, width='stretch'):
                            st.session_state.letra_selecionada = letra
                            st.rerun()
                
                st.write("---")
                alunos_filtrados = [a for a in lista_alunos if a['nome'].upper().startswith(st.session_state.letra_selecionada)]
                
                if alunos_filtrados:
                    col1, col2, col3 = st.columns(3)
                    for idx, aluno in enumerate(alunos_filtrados):
                        alvo_col = col1 if idx % 3 == 0 else (col2 if idx % 3 == 1 else col3)
                        with alvo_col:
                            primeiro_nome = aluno['nome'].split()[0]
                            if st.button(f"👤 {primeiro_nome}", help=aluno['nome'], width='stretch', key=f"btn_{aluno['id']}"):
                                try:
                                    client.table("presenca").insert({
                                        "aluno_id": int(aluno['id']), "data": dados_aula["data_encontro"], "status": "Presente",
                                        "ano": dados_modulo["ano"], "modulo_numero": str(dados_modulo["numero"]),
                                        "modulo_nome": dados_modulo["nome"], "professor": dados_modulo["professor"]
                                    }).execute()
                                    st.success(f"✅ Registrado: {primeiro_nome}!")
                                except Exception:
                                    st.warning("Já registrado!")
                else:
                    st.info("Nenhum aluno com esta inicial.")
            else:
                nomes_alunos = [aluno['nome'] for aluno in lista_alunos]
                st.markdown("### 👋 Digite seu nome para filtrar:")
                irmao_selecionado = st.selectbox("Clique abaixo e comece a digitar seu nome:", options=nomes_alunos, index=None, placeholder="Digite seu nome aqui...")
                
                if irmao_selecionado:
                    if st.button(f"Confirmar Presença para: {irmao_selecionado}", type="primary", width='stretch'):
                        aluno_id = next(aluno['id'] for aluno in lista_alunos if aluno['nome'] == irmao_selecionado)
                        try:
                            client.table("presenca").insert({
                                "aluno_id": int(aluno_id), "data": dados_aula["data_encontro"], "status": "Presente",
                                "ano": dados_modulo["ano"], "modulo_numero": str(dados_modulo["numero"]),
                                "modulo_nome": dados_modulo["nome"], "professor": dados_modulo["professor"]
                            }).execute()
                            st.success(f"✅ Sucesso! Presença registrada para {irmao_selecionado}.")
                            st.balloons()
                        except Exception:
                            st.warning("Você já registrou sua presença hoje!")
        else:
            st.warning("Não existem alunos matriculados neste módulo.")
    else:
        st.info("A chamada não está aberta no momento.")

elif menu == "Painel do Instrutor":
    st.header("⚙️ Controle do Instrutor")
    if "instrutor_autenticado" not in st.session_state:
        st.session_state.instrutor_autenticado = False

    if not st.session_state.instrutor_autenticado:
        senha_digitada = st.text_input("Digite a senha de acesso do Instrutor:", type="password")
        senha_correta = st.secrets["credentials"]["senha_instructor" if "senha_instructor" in st.secrets["credentials"] else "senha_instrutor"]
        if senha_digitada == senha_correta:
            st.session_state.instrutor_autenticado = True
            st.rerun()
        elif senha_digitada != "":
            st.error("Senha incorreta. Acesso negado.")
    else:
        if st.sidebar.button("🔒 Sair do Painel"):
            st.session_state.instrutor_autenticado = False
            st.rerun()
            
        tab1, tab2, tab3, tab4 = st.tabs(["📊 Relatório de Presenças", "🎮 Controle de Chamada", "📖 Cadastrar Módulos", "👥 Alunos e Matrículas"])
        res_modulos = client.table("modulos").select("*").order("ano", desc=True).order("numero").execute()
        
        with tab1:
            st.subheader("📊 Diário de Classe - Visão Geral por Matéria")
            if res_modulos.data:
                dict_filtro_mod = {f"Mód {m['numero']} - {m['nome']}": m for m in res_modulos.data}
                mod_escolhido_txt = st.selectbox("Filtrar Relatório por Matéria:", options=list(dict_filtro_mod.keys()))
                modulo_objeto = dict_filtro_mod[mod_escolhido_txt]
                codigo_mod_sel = modulo_objeto['numero']
                
                res_relatorio = client.table("presenca").select("data, status, alunos(nome)").eq("modulo_numero", codigo_mod_sel).execute()
                
                if res_relatorio.data:
                    dados_brutos = [{
                        "Data": pd.to_datetime(item["data"]).strftime('%d/%m/%Y'),
                        "Aluno": item["alunos"]["nome"] if item["alunos"] else "N/A",
                        "Status": 1
                    } for item in res_relatorio.data]
                    
                    df_base = pd.DataFrame(dados_brutos)
                    df_exibicao = df_base.pivot(index="Aluno", columns="Data", values="Status").fillna(0)
                    
                    res_total_mat = client.table("matriculas").select("id").eq("modulo_id", modulo_objeto['id']).execute()
                    total_matriculados = len(res_total_mat.data) if res_total_mat.data else len(df_exibicao.index)
                    if total_matriculados == 0: total_matriculados = 1
                    
                    total_dias = len(df_exibicao.columns)
                    participacao_alunos = (df_exibicao.sum(axis=1) / total_dias) * 100
                    engajamento_encontro = (df_exibicao.sum(axis=0) / total_matriculados) * 100
                    media_geral_turma = engajamento_encontro.mean()
                    
                    df_visual = df_exibicao.replace({1: "🟢 Presente", 0: "—"})
                    df_visual["% Participação"] = participacao_alunos.map("{:.1f}%".format)
                    
                    c1, c2 = st.columns([1, 2])
                    with c1:
                        st.write("")
                        st.metric(label="Engajamento Geral da Turma", value=f"{media_geral_turma:.1f}%")
                        st.metric(label="Total de Aulas Registradas", value=f"{total_dias} encontros")
                    with c2:
                        fig, ax = plt.subplots(figsize=(5, 2.3))
                        ax.bar(engajamento_encontro.index, engajamento_encontro.values, color='#1B365D', width=0.4)
                        ax.set_ylabel('% Engajamento', color='#5A6A85', fontsize=8)
                        ax.set_ylim(0, 105)
                        ax.tick_params(axis='both', labelsize=8, colors='#5A6A85')
                        ax.grid(axis='y', linestyle='--', alpha=0.5)
                        st.pyplot(fig)
                    
                    st.markdown("### Grade de Frequência Consolidada")
                    st.dataframe(df_visual, width='stretch')
                    
                    img_buf = io.BytesIO()
                    fig.savefig(img_buf, format='png', bbox_inches='tight', dpi=200)
                    img_buf.seek(0)
                    img_b64 = base64.b64encode(img_buf.read()).decode('utf-8')
                    
                    linhas_html = ""
                    for idx, (aluno_nome, row_data) in enumerate(df_visual.iterrows()):
                        classe_linha = "row-even" if idx % 2 == 0 else "row-odd"
                        colunas_presenca = "".join([f"<td>{row_data[d]}</td>" for d in df_exibicao.columns])
                        linhas_html += f"""
                        <tr class="{classe_linha}">
                            <td style="text-align: left; padding-left: 10px;">{aluno_nome}</td>
                            {colunas_presenca}
                            <td class="pct-cell">{row_data["% Participação"]}</td>
                        </tr>
                        """
                    
                    colunas_header_html = "".join([f"<th>{d}</th>" for d in df_exibicao.columns])
                    valores_totais_encontro = "".join([f"<td>{val:.1f}%</td>" for val in engajamento_encontro.values])
                    
                    html_template = f"""
                    <html>
                    <head>
                        <style>
                            @page {{ size: A4; margin: 15mm 12mm; }}
                            * {{ box-sizing: border-box; font-family: 'Arial', sans-serif; }}
                            body {{ color: #333333; margin: 0; }}
                            .header {{ background-color: #1B365D; color: white; padding: 18px; border-radius: 4px; margin-bottom: 15px; }}
                            .meta-table {{ width: 100%; border-collapse: collapse; font-size: 10pt; margin-bottom: 15px; }}
                            .meta-label {{ font-weight: bold; color: #5A6A85; width: 15%; }}
                            .meta-val {{ color: #1B365D; font-weight: bold; }}
                            .dash-box {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
                            .card {{ background-color: #F4F7FA; border: 1px solid #E2E8F0; padding: 12px; text-align: center; width: 40%; vertical-align: middle; }}
                            .data-table {{ width: 100%; border-collapse: collapse; font-size: 9pt; }}
                            .data-table th {{ background-color: #1B365D; color: white; padding: 8px; font-weight: bold; }}
                            .data-table td {{ padding: 6px; border-bottom: 1px solid #E2E8F0; text-align: center; }}
                            .row-odd {{ background-color: #F4F7FA; }}
                            .pct-cell {{ font-weight: bold; color: #1B365D; background-color: rgba(74,144,226,0.06); }}
                            .row-total td {{ font-weight: bold; color: #1B365D; background-color: #E6FFFA; border-top: 1.5px solid #1B365D; border-bottom: 2px double #1B365D; }}
                        </style>
                    </head>
                    <body>
                        <div class="header">
                            <h2 style="margin:0; font-size:16pt;">CCM - DIÁRIO DE CLASSE PREMIUM (PDF)</h2>
                        </div>
                        <table class="meta-table">
                            <tr><td class="meta-label">MÓDULO:</td><td class="meta-val">{modulo_objeto['numero']} - {modulo_objeto['nome']}</td></tr>
                            <tr><td class="meta-label">PROFESSOR:</td><td class="meta-val">{modulo_objeto['professor']}</td></tr>
                        </table>
                        <table class="dash-box">
                            <tr>
                                <td class="card">
                                    <div style="font-size:8pt; color:#5A6A85; font-weight:bold;">ENGAJAMENTO GERAL</div>
                                    <div style="font-size:20pt; font-weight:bold; color:#1B365D;">{media_geral_turma:.1f}%</div>
                                </td>
                                <td style="width:5%;"></td>
                                <td class="card" style="width:55%; padding:2px;">
                                    <img src="data:image/png;base64,{img_b64}" style="width:100%; height:80px; object-fit:contain;"/>
                                </td>
                            </tr>
                        </table>
                        <table class="data-table">
                            <thead>
                                <tr><th style="text-align:left; padding-left:10px;">Nome do Aluno</th>{colunas_header_html}<th>% Frequência</th></tr>
                            </thead>
                            <tbody>
                                {linhas_html}
                                <tr class="row-total">
                                    <td style="text-align:right; padding-right:10px;">Média Diária:</td>
                                    {valores_totais_encontro}
                                    <td style="background-color:#4A90E2; color:white;">{media_geral_turma:.1f}%</td>
                                </tr>
                            </tbody>
                        </table>
                    </body>
                    </html>
                    """
                    
                    try:
                        from weasyprint import HTML
                        pdf_buffer = io.BytesIO()
                        HTML(string=html_template).write_pdf(pdf_buffer)
                        pdf_data = pdf_buffer.getvalue()
                        
                        st.write("")
                        st.download_button(
                            label="📥 Baixar Diário de Presença Premium em PDF",
                            data=pdf_data,
                            file_name=f"Diario_Premium_{codigo_mod_sel}.pdf",
                            mime="application/pdf",
                            width='stretch'
                        )
                    except Exception as e:
                        st.error(f"Erro na geração do PDF: {e}")
                else:
                    st.info(f"Nenhum registro de presença para o módulo {codigo_mod_sel}.")
            else:
                st.info("Cadastre matérias/módulos para começar a gerar relatórios.")

        with tab2:
            st.subheader("Gerenciar Encontro do Dia")
            status_atual = client.table("chamada_ativa").select("*, modulos(*)").eq("id", 1).execute()
            dados_atuais = status_atual.data[0] if status_atual.data else {}
            esta_aberta = dados_atuais.get("aberta", False)
            ux_salva = dados_atuais.get("tipo_ux", "Lista com Busca")
            
            if res_modulos.data:
                dict_modulos = {f"Mód {m['numero']} - {m['nome']} ({m['professor']})": m['id'] for m in res_modulos.data}
                id_modulo_saved = dados_atuais.get("modulo_id")
                idx_padrao = 0
                if id_modulo_saved:
                    for idx, m_id in enumerate(dict_modulos.values()):
                        if m_id == id_modulo_saved:
                            idx_padrao = idx
                            break
                
                modulo_selecionado_texto = st.selectbox("Selecione o Módulo Ativo do Curso:", options=list(dict_modulos.keys()), index=idx_padrao, key="mod_chamada")
                id_modulo_selecionado = dict_modulos[modulo_selecionado_texto]
                data_aula = st.date_input("Data do Encontro", date.today())
                
                st.write("---")
                opcoes_ux = ["Lista com Busca", "Botões com Iniciais (A-Z)"]
                idx_ux = opcoes_ux.index(ux_salva) if ux_salva in opcoes_ux else 0
                ux_selecionada = st.radio("Escolha como os alunos marcarão a presença no celular:", options=opcoes_ux, index=idx_ux)
                
                if esta_aberta:
                    st.success(f"🟢 **Chamada ABERTA**")
                    if ux_selecionada != ux_salva:
                        client.table("chamada_ativa").upsert({"id": 1, "data_encontro": data_aula.isoformat(), "aberta": True, "modulo_id": int(id_modulo_selecionado), "tipo_ux": ux_selecionada}).execute()
                        st.rerun()
                    if st.button("Fechar Chamada Agora", type="primary"):
                        client.table("chamada_ativa").upsert({"id": 1, "data_encontro": data_aula.isoformat(), "aberta": False, "modulo_id": int(id_modulo_selecionado), "tipo_ux": ux_selecionada}).execute()
                        st.rerun()
                else:
                    st.error("🔴 **Chamada FECHADA**")
                    if st.button("Abrir Chamada para os Alunos", type="primary"):
                        client.table("chamada_ativa").upsert({"id": 1, "data_encontro": data_aula.isoformat(), "aberta": True, "modulo_id": int(id_modulo_selecionado), "tipo_ux": ux_selecionada}).execute()
                        st.rerun()

        with tab3:
            st.subheader("📖 Cadastrar Novo Módulo do CCM")
            with st.form("form_modulo"):
                ano_mod = st.number_input("Ano Letivo", min_value=2020, max_value=2100, value=date.today().year)
                code_mod = st.text_input("Código do Módulo [MDAA##]")
                nome_mod = st.text_input("Nome do Módulo / Matéria")
                prof_mod = st.text_input("Nome do Professor(a)")
                if st.form_submit_button("Salvar Módulo"):
                    if code_mod and nome_mod and prof_mod:
                        try:
                            client.table("modulos").insert({"ano": int(ano_mod), "numero": code_mod.strip().upper(), "nome": nome_mod.strip(), "professor": prof_mod.strip()}).execute()
                            st.success("Módulo cadastrado!")
                            st.rerun()
                        except Exception:
                            st.error("Erro ou código duplicado.")

        with tab4:
            st.subheader("1. Base Geral: Cadastro Permanente de Alunos")
            nome_novo = st.text_input("Nome Completo")
            if st.button("Salvar Cadastro na Base"):
                if nome_novo:
                    client.table("alunos").insert({"nome": nome_novo.strip()}).execute()
                    st.success("Aluno adicionado permanente à base!")
                    st.rerun()
            
            st.write("---")
            st.subheader("2. Efetivar Nova Matrícula em um Módulo")
            res_todos_alunos = client.table("alunos").select("id, nome").order("nome").execute()
            if res_todos_alunos.data and res_modulos.data:
                dict_todos_alunos = {aluno['nome']: aluno['id'] for aluno in res_todos_alunos.data}
                aluno_para_matricular = st.selectbox("Selecione a pessoa:", options=list(dict_todos_alunos.keys()))
                dict_modulos_mat = {f"Mód {m['numero']} - {m['nome']}": m['id'] for m in res_modulos.data}
                modulo_para_matricular_texto = st.selectbox("Selecione o Módulo de Destino:", options=list(dict_modulos_mat.keys()))
                id_mod_mat = dict_modulos_mat[modulo_para_matricular_texto]
                
                if st.button("Efetivar Matrícula no Módulo"):
                    try:
                        client.table("matriculas").insert({"aluno_id": int(dict_todos_alunos[aluno_para_matricular]), "modulo_id": int(id_mod_mat)}).execute()
                        st.success("Matrícula vinculada com sucesso!")
                        st.rerun()
                    except Exception:
                        st.warning("Aluno já matriculado neste módulo.")
            
            st.write("---")
            st.subheader("3. Verificar Alunos Matriculados por Módulo")
            if res_modulos.data:
                dict_ver_matriculas = {f"Mód {m['numero']} - {m['nome']}": m['id'] for m in res_modulos.data}
                mod_escolhido_ver = st.selectbox("Selecione o Módulo para ver os alunos ativos:", options=list(dict_ver_matriculas.keys()), key="sb_ver_mat")
                id_mod_ver = dict_ver_matriculas[mod_escolhido_ver]
                
                res_lista_mat_filtrada = client.table("matriculas").select("alunos(nome)").eq("modulo_id", id_mod_ver).execute()
                
                if res_lista_mat_filtrada.data:
                    alunos_encontrados = [{"Nome do Aluno Ativo": m["alunos"]["nome"]} for m in res_lista_mat_filtrada.data if m["alunos"]]
                    df_alunos_mat = pd.DataFrame(alunos_encontrados).sort_values(by="Nome do Aluno Ativo").reset_index(drop=True)
                    df_alunos_mat.index += 1
                    
                    st.write("")
                    with st.container(border=True):
                        st.markdown(f"📊 **Total de alunos matriculados nesta matéria:** `{len(df_alunos_mat)}`")
                        st.write("")
                        st.dataframe(df_alunos_mat, width='stretch', height=400)
                else:
                    st.info("Nenhum aluno matriculado especificamente neste módulo ainda.")
            else:
                st.info("Cadastre módulos primeiro.")
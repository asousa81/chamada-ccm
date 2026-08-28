import streamlit as st
import pandas as pd
from datetime import date
from supabase import create_client, Client
import io
import base64
import hmac
import difflib
import httpx
from collections import Counter
import matplotlib.pyplot as plt
from postgrest.exceptions import APIError

st.set_page_config(page_title="Presença CCM", layout="centered")

# Erros de rede (host que não resolve, timeout, conexão derrubada).
# Precisam ser tratados separadamente dos erros de banco: o Supabase só é
# acessado de fato na primeira consulta, nunca no create_client().
ERROS_CONEXAO = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


class CredenciaisAusentes(Exception):
    """Secrets do Supabase não configurados no ambiente."""


def _config_supabase() -> dict:
    return dict(st.secrets.get("connections", {}).get("supabase", {}))


def _url_mascarada() -> str:
    """Mostra a URL sem revelar o ref completo do projeto — a tela de erro é pública."""
    url = str(_config_supabase().get("url", "")).strip()
    if not url:
        return "(não informada)"
    ref = url.replace("https://", "").split(".")[0]
    return url.replace(ref, ref[:4] + "…" + ref[-2:]) if len(ref) > 8 else url


def duplicado(erro: APIError) -> bool:
    """True quando o Postgres recusou o insert por violação de chave única."""
    return getattr(erro, "code", None) == "23505" or "duplicate key" in (erro.message or "").lower()


def avisar_conexao(contexto: str = "") -> None:
    st.error(
        f"🔌 Sem conexão com o banco de dados{(' ao ' + contexto) if contexto else ''}. "
        "Tente novamente em instantes; se persistir, avise o responsável pelo app."
    )


def normalizar(texto: str) -> str:
    """Colapsa espaços duplicados e sobras nas pontas."""
    return " ".join(str(texto).split())


@st.cache_data(ttl=600, show_spinner=False)
def tem_coluna_origem() -> bool:
    """A coluna `origem` é opcional: o app funciona antes e depois da migração."""
    try:
        client.table("presenca").select("origem").limit(1).execute()
        return True
    except APIError:
        return False


def montar_presenca(aluno_id, data_encontro, modulo: dict, origem: str) -> dict:
    registro = {
        "aluno_id": int(aluno_id),
        "data": data_encontro,
        "status": "Presente",
        "ano": modulo["ano"],
        "modulo_numero": str(modulo["numero"]),
        "modulo_nome": modulo["nome"],
        "professor": modulo["professor"],
    }
    if tem_coluna_origem():
        registro["origem"] = origem
    return registro


def cadastrar_aluno(nome: str) -> None:
    try:
        client.table("alunos").insert({"nome": nome}).execute()
        st.session_state.pop("aluno_pendente", None)
        st.session_state.pop("aluno_parecidos", None)
        st.session_state["flash_alunos"] = f"✅ {nome} adicionado à base."
        st.rerun()
    except APIError as erro:
        if duplicado(erro):
            st.error("Já existe um cadastro com este nome.")
        else:
            st.error(f"Não foi possível cadastrar: {erro.message}")
    except ERROS_CONEXAO:
        avisar_conexao("cadastrar o aluno")


@st.cache_resource(show_spinner="Conectando ao banco de dados...")
def init_supabase() -> Client:
    conf = _config_supabase()
    url = str(conf.get("url", "")).strip().rstrip("/")
    key = str(conf.get("key", "")).strip()

    if not url or not key:
        raise CredenciaisAusentes()

    cliente = create_client(url, key)
    # Consulta de sanidade: create_client() não abre conexão nenhuma, então sem
    # este ping o app só descobriria a falha lá na frente, com stack trace na tela.
    cliente.table("chamada_ativa").select("id").limit(1).execute()
    return cliente


try:
    client = init_supabase()
except CredenciaisAusentes:
    st.error("⚙️ Credenciais do Supabase não configuradas.")
    st.caption(
        "Em Settings → Secrets do app, informe a seção `[connections.supabase]` "
        "com as chaves `url` e `key`."
    )
    st.stop()
except ERROS_CONEXAO:
    st.error("🔌 O app está fora do ar no momento — o banco de dados não respondeu.")
    st.info("Se você é aluno, avise o instrutor. A chamada pode ser feita na lista impressa.")
    with st.expander("Detalhes técnicos (para o responsável pelo app)"):
        st.markdown(
            f"""
`httpx.ConnectError` — o host do Supabase não resolveu em DNS.
URL configurada: `{_url_mascarada()}`

**O que verificar, nesta ordem:**

1. **Projeto pausado no Supabase.** No plano gratuito o projeto hiberna após dias sem uso
   e o subdomínio deixa de existir. Basta restaurar pelo painel.
2. **URL do projeto.** Deve ser exatamente a de *Project Settings → API → Project URL*,
   no formato `https://<ref>.supabase.co` — sem `/rest/v1` no fim.
3. Depois de corrigir os Secrets, use **Reboot app** no painel do Streamlit.
            """
        )
    st.stop()
except APIError as erro:
    st.error("🗄️ O app está fora do ar no momento — o banco recusou a consulta.")
    with st.expander("Detalhes técnicos (para o responsável pelo app)"):
        st.markdown(
            f"""
Mensagem do banco: `{erro.message}`

Confira se a tabela `chamada_ativa` existe e se a chave usada tem permissão de
leitura (políticas de RLS).
            """
        )
    st.stop()
except Exception as erro:
    st.error(f"Falha inesperada ao conectar ao Supabase: {erro}")
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
                                    client.table("presenca").insert(
                                        montar_presenca(aluno['id'], dados_aula["data_encontro"], dados_modulo, "aluno")
                                    ).execute()
                                    st.success(f"✅ Registrado: {primeiro_nome}!")
                                except APIError as erro:
                                    if duplicado(erro):
                                        st.warning("Já registrado!")
                                    else:
                                        st.error(f"Não foi possível registrar: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("registrar a presença")
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
                            client.table("presenca").insert(
                                montar_presenca(aluno_id, dados_aula["data_encontro"], dados_modulo, "aluno")
                            ).execute()
                            st.success(f"✅ Sucesso! Presença registrada para {irmao_selecionado}.")
                            st.balloons()
                        except APIError as erro:
                            if duplicado(erro):
                                st.warning("Você já registrou sua presença hoje!")
                            else:
                                st.error(f"Não foi possível registrar: {erro.message}")
                        except ERROS_CONEXAO:
                            avisar_conexao("registrar a presença")
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
        credenciais = st.secrets.get("credentials", {})
        senha_correta = credenciais.get("senha_instrutor") or credenciais.get("senha_instructor")
        if not senha_correta:
            st.error("⚙️ Senha do instrutor não configurada nos Secrets (`[credentials] senha_instrutor`).")
            st.stop()
        if senha_digitada and hmac.compare_digest(senha_digitada, str(senha_correta)):
            st.session_state.instrutor_autenticado = True
            st.rerun()
        elif senha_digitada != "":
            st.error("Senha incorreta. Acesso negado.")
    else:
        if st.sidebar.button("🔒 Sair do Painel"):
            st.session_state.instrutor_autenticado = False
            st.rerun()
            
        tab_live, tab1, tab2, tab3, tab4 = st.tabs(["🙋 Chamada ao Vivo", "📊 Relatório de Presenças", "🎮 Controle de Chamada", "📖 Cadastrar Módulos", "👥 Alunos e Matrículas"])
        res_modulos = client.table("modulos").select("*").order("ano", desc=True).order("numero").execute()

        with tab_live:
            st.subheader("🙋 Chamada ao Vivo")
            st.caption(
                "Para o instrutor conduzir a chamada no próprio aparelho: toque no nome para "
                "marcar presença, toque de novo para desmarcar."
            )

            try:
                chamada_atual = client.table("chamada_ativa").select("*, modulos(*)").eq("id", 1).execute().data
            except ERROS_CONEXAO:
                avisar_conexao("carregar a chamada")
                st.stop()

            dados_chamada = chamada_atual[0] if chamada_atual else {}
            modulo_ao_vivo = dados_chamada.get("modulos")

            if not dados_chamada.get("aberta") or not modulo_ao_vivo:
                st.info("Nenhuma chamada aberta no momento. Abra na aba **🎮 Controle de Chamada**.")
            else:
                data_encontro = dados_chamada["data_encontro"]
                st.markdown(
                    f"**Mód {modulo_ao_vivo['numero']} — {modulo_ao_vivo['nome']}** · "
                    f"{pd.to_datetime(data_encontro).strftime('%d/%m/%Y')}"
                )

                try:
                    matriculados_ao_vivo = client.table("matriculas").select("alunos(id, nome)").eq(
                        "modulo_id", dados_chamada["modulo_id"]
                    ).execute().data or []
                    colunas_presenca = "id, aluno_id" + (", origem" if tem_coluna_origem() else "")
                    ja_registrados = client.table("presenca").select(colunas_presenca).eq(
                        "data", data_encontro
                    ).eq("modulo_numero", str(modulo_ao_vivo["numero"])).execute().data or []
                except APIError as erro:
                    st.error(f"Não foi possível carregar a chamada: {erro.message}")
                    st.stop()
                except ERROS_CONEXAO:
                    avisar_conexao("carregar a lista da turma")
                    st.stop()

                turma = sorted(
                    (m["alunos"] for m in matriculados_ao_vivo if m["alunos"]),
                    key=lambda a: a["nome"],
                )
                registro_por_aluno = {r["aluno_id"]: r for r in ja_registrados}

                if not turma:
                    st.warning("Nenhum aluno matriculado neste módulo.")
                else:
                    total_presentes = sum(1 for a in turma if a["id"] in registro_por_aluno)
                    st.progress(
                        total_presentes / len(turma),
                        text=f"{total_presentes} de {len(turma)} presentes",
                    )

                    filtro_ao_vivo = normalizar(st.text_input(
                        "🔎 Filtrar", placeholder="Digite parte do nome...", key="filtro_ao_vivo"
                    )).lower()
                    visiveis = [a for a in turma if filtro_ao_vivo in a["nome"].lower()] if filtro_ao_vivo else turma

                    col_esq, col_dir = st.columns(2)
                    for indice, aluno_live in enumerate(visiveis):
                        registro_live = registro_por_aluno.get(aluno_live["id"])
                        marcado = registro_live is not None
                        origem_registro = (registro_live or {}).get("origem", "aluno")
                        icone = ("✅" if origem_registro == "aluno" else "👤") if marcado else "⬜"

                        with (col_esq if indice % 2 == 0 else col_dir):
                            if st.button(
                                f"{icone} {aluno_live['nome']}",
                                key=f"live_{aluno_live['id']}",
                                width='stretch',
                            ):
                                try:
                                    if marcado:
                                        client.table("presenca").delete().eq("id", registro_live["id"]).execute()
                                    else:
                                        client.table("presenca").insert(
                                            montar_presenca(aluno_live["id"], data_encontro, modulo_ao_vivo, "instrutor")
                                        ).execute()
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível atualizar: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("atualizar a presença")

                    st.caption("✅ registrado pelo próprio aluno · 👤 marcado pelo instrutor · ⬜ ausente")
        
        with tab1:
            st.subheader("📊 Diário de Classe - Visão Geral por Matéria")
            if res_modulos.data:
                dict_filtro_mod = {f"Mód {m['numero']} - {m['nome']}": m for m in res_modulos.data}
                mod_escolhido_txt = st.selectbox("Filtrar Relatório por Matéria:", options=list(dict_filtro_mod.keys()))
                modulo_objeto = dict_filtro_mod[mod_escolhido_txt]
                codigo_mod_sel = modulo_objeto['numero']
                id_modulo_sel = modulo_objeto['id']
                
                # 1. Busca a lista oficial de TODOS os alunos matriculados nesta matéria
                res_total_mat = client.table("matriculas").select("alunos(nome)").eq("modulo_id", id_modulo_sel).execute()
                lista_todos_matriculados = sorted([m["alunos"]["nome"] for m in res_total_mat.data if m["alunos"]]) if res_total_mat.data else []
                total_matriculados = len(lista_todos_matriculados)
                if total_matriculados == 0: total_matriculados = 1
                
                # 2. Busca o histórico de presenças registradas
                res_relatorio = client.table("presenca").select("data, status, alunos(nome)").eq(
                    "modulo_numero", codigo_mod_sel
                ).eq("ano", modulo_objeto["ano"]).execute()
                
                if res_relatorio.data and lista_todos_matriculados:
                    # Encontra todas as datas únicas que já tiveram chamada nessa matéria
                    datas_com_chamada = sorted(list(set([pd.to_datetime(item["data"]).strftime('%d/%m/%Y') for item in res_relatorio.data])))
                    
                    # Cria um conjunto rápido de cruzamento (Aluno, Data) para identificar presenças legítimas
                    presencas_confirmadas = set(
                        (item["alunos"]["nome"], pd.to_datetime(item["data"]).strftime('%d/%m/%Y'))
                        for item in res_relatorio.data if item["alunos"]
                    )
                    
                    # 3. Monta a matriz garantindo que TODOS os matriculados apareçam em TODAS as datas
                    dados_grade = []
                    for aluno in lista_todos_matriculados:
                        for d_chamada in datas_com_chamada:
                            status_presenca = 1 if (aluno, d_chamada) in presencas_confirmadas else 0
                            dados_grade.append({"Aluno": aluno, "Data": d_chamada, "Status": status_presenca})
                    
                    df_base = pd.DataFrame(dados_grade)
                    df_exibicao = df_base.pivot_table(index="Aluno", columns="Data", values="Status", aggfunc="max").fillna(0)
                    
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
                    
                    if total_dias > 0:
                        ultima_data_col = df_exibicao.columns[-1]
                        serie_ultima_aula = df_exibicao[ultima_data_col]
                        nomes_faltantes = serie_ultima_aula[serie_ultima_aula == 0].index.tolist()
                        
                        st.write("")
                        with st.expander(f"🔍 Ver Alunos Faltantes no último encontro ({ultima_data_col})", expanded=False):
                            if nomes_faltantes:
                                st.markdown(f"🔴 **Total de Faltas:** `{len(nomes_faltantes)}` alunos")
                                df_faltantes = pd.DataFrame({"Aluno Ausente": sorted(nomes_faltantes)})
                                df_faltantes.index += 1
                                st.dataframe(df_faltantes, width='stretch', height=250)
                            else:
                                st.success("🙌 Glória a Deus! 100% de presença no último encontro!")
                    
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
                            <h2 style="margin:0; font-size:16pt;">CCM - DIÁRIO DE CLASSE</h2>
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
                            label="📥 Baixar Diário de Presença",
                            data=pdf_data,
                            file_name=f"Diario_Premium_{codigo_mod_sel}.pdf",
                            mime="application/pdf",
                            width='stretch'
                        )
                    except Exception as e:
                        st.error(f"Erro na geração do PDF: {e}")
                else:
                    st.info(f"Nenhum registro completo ou alunos matriculados encontrados para o módulo {codigo_mod_sel}.")
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
            st.subheader("📖 Módulos do CCM")

            if st.session_state.get("flash_modulos"):
                st.success(st.session_state.pop("flash_modulos"))

            modulos_lista = res_modulos.data or []
            chaves_modulos = {
                (str(m["numero"]).strip().upper(), int(m["ano"])): m["id"] for m in modulos_lista
            }

            aba_novo_mod, aba_gerir_mod = st.tabs(["➕ Cadastrar", "✏️ Gerenciar"])

            with aba_novo_mod:
                with st.form("form_modulo", clear_on_submit=True):
                    ano_mod = st.number_input("Ano Letivo", min_value=2020, max_value=2100, value=date.today().year)
                    code_mod = st.text_input("Código do Módulo [MDAA##]")
                    nome_mod = st.text_input("Nome do Módulo / Matéria")
                    prof_mod = st.text_input("Nome do Professor(a)")
                    criar_modulo = st.form_submit_button("Salvar Módulo", type="primary")

                if criar_modulo:
                    codigo_limpo = normalizar(code_mod).upper()
                    nome_limpo_mod = normalizar(nome_mod)
                    prof_limpo = normalizar(prof_mod)

                    if not (codigo_limpo and nome_limpo_mod and prof_limpo):
                        st.warning("Preencha código, nome e professor.")
                    elif (codigo_limpo, int(ano_mod)) in chaves_modulos:
                        st.error(f"Já existe o módulo {codigo_limpo} no ano letivo {int(ano_mod)}.")
                    else:
                        try:
                            client.table("modulos").insert({
                                "ano": int(ano_mod), "numero": codigo_limpo,
                                "nome": nome_limpo_mod, "professor": prof_limpo,
                            }).execute()
                            st.session_state["flash_modulos"] = f"✅ Módulo {codigo_limpo} cadastrado."
                            st.rerun()
                        except APIError as erro:
                            if duplicado(erro):
                                st.error("Já existe um módulo com este código.")
                            else:
                                st.error(f"Não foi possível cadastrar: {erro.message}")
                        except ERROS_CONEXAO:
                            avisar_conexao("cadastrar o módulo")

            with aba_gerir_mod:
                if not modulos_lista:
                    st.info("Nenhum módulo cadastrado ainda.")
                else:
                    try:
                        vinculos_mod = client.table("matriculas").select("modulo_id").execute().data or []
                        presencas_mod = client.table("presenca").select("modulo_numero, ano").execute().data or []
                        chamada_para_travar = client.table("chamada_ativa").select("modulo_id, aberta").eq("id", 1).execute().data or []
                    except APIError as erro:
                        st.error(f"Não foi possível carregar os vínculos: {erro.message}")
                        st.stop()
                    except ERROS_CONEXAO:
                        avisar_conexao("carregar os vínculos dos módulos")
                        st.stop()

                    qtd_matriculas_mod = Counter(v["modulo_id"] for v in vinculos_mod)
                    qtd_presencas_mod = Counter(
                        (str(p["modulo_numero"]).strip().upper(), int(p["ano"]))
                        for p in presencas_mod if p.get("modulo_numero") and p.get("ano") is not None
                    )
                    modulo_em_chamada = (
                        chamada_para_travar[0].get("modulo_id")
                        if chamada_para_travar and chamada_para_travar[0].get("aberta") else None
                    )

                    rotulo_modulos = {
                        f"{m['ano']} · Mód {m['numero']} - {m['nome']}": m for m in modulos_lista
                    }
                    escolha_mod = st.selectbox(
                        "Selecione o módulo",
                        options=list(rotulo_modulos.keys()),
                        index=None,
                        placeholder="Digite para buscar...",
                        key="sel_gerir_modulo",
                    )

                    if escolha_mod:
                        mod_alvo = rotulo_modulos[escolha_mod]
                        codigo_atual = str(mod_alvo["numero"]).strip().upper()
                        ano_atual = int(mod_alvo["ano"])
                        n_mat_mod = qtd_matriculas_mod.get(mod_alvo["id"], 0)
                        n_pres_mod = qtd_presencas_mod.get((codigo_atual, ano_atual), 0)

                        with st.container(border=True):
                            st.markdown(f"**Mód {mod_alvo['numero']} — {mod_alvo['nome']}**")
                            st.caption(
                                f"Ano {ano_atual} · Professor(a): {mod_alvo['professor']} · "
                                f"{n_mat_mod} matriculado(s) · {n_pres_mod} presença(s) no histórico"
                            )

                        st.markdown("**Corrigir dados**")
                        chave_edicao = f"{mod_alvo['id']}"
                        novo_ano_mod = st.number_input(
                            "Ano Letivo", min_value=2020, max_value=2100, value=ano_atual,
                            key=f"edit_ano_{chave_edicao}",
                        )
                        novo_codigo_mod = st.text_input(
                            "Código do Módulo", value=mod_alvo["numero"], key=f"edit_cod_{chave_edicao}"
                        )
                        novo_nome_mod = st.text_input(
                            "Nome do Módulo / Matéria", value=mod_alvo["nome"], key=f"edit_nome_{chave_edicao}"
                        )
                        novo_prof_mod = st.text_input(
                            "Nome do Professor(a)", value=mod_alvo["professor"], key=f"edit_prof_{chave_edicao}"
                        )

                        codigo_editado = normalizar(novo_codigo_mod).upper()
                        ano_editado = int(novo_ano_mod)
                        muda_identidade = (codigo_editado, ano_editado) != (codigo_atual, ano_atual)

                        if muda_identidade and n_pres_mod:
                            st.warning(
                                "Alterar código ou ano muda a identidade do módulo. As presenças já "
                                "registradas apontam para o código antigo e serão atualizadas junto, "
                                "senão o histórico ficaria órfão no relatório."
                            )

                        propagar = st.checkbox(
                            "Aplicar a correção também ao histórico de presenças",
                            value=True,
                            disabled=muda_identidade,
                            help="Obrigatório quando o código ou o ano mudam.",
                            key=f"edit_prop_{chave_edicao}",
                        ) or muda_identidade

                        if st.button("Salvar alterações", type="primary", key=f"btn_edit_mod_{chave_edicao}"):
                            nome_editado = normalizar(novo_nome_mod)
                            prof_editado = normalizar(novo_prof_mod)
                            conflito = chaves_modulos.get((codigo_editado, ano_editado))

                            if not (codigo_editado and nome_editado and prof_editado):
                                st.warning("Código, nome e professor não podem ficar vazios.")
                            elif conflito and conflito != mod_alvo["id"]:
                                st.error(f"Já existe outro módulo {codigo_editado} no ano {ano_editado}.")
                            else:
                                try:
                                    client.table("modulos").update({
                                        "ano": ano_editado, "numero": codigo_editado,
                                        "nome": nome_editado, "professor": prof_editado,
                                    }).eq("id", int(mod_alvo["id"])).execute()

                                    if propagar and n_pres_mod:
                                        client.table("presenca").update({
                                            "modulo_numero": codigo_editado, "modulo_nome": nome_editado,
                                            "professor": prof_editado, "ano": ano_editado,
                                        }).eq("modulo_numero", codigo_atual).eq("ano", ano_atual).execute()

                                    st.session_state["flash_modulos"] = f"✅ Módulo {codigo_editado} atualizado."
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível atualizar: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("atualizar o módulo")

                        st.write("---")
                        st.markdown("**Excluir módulo**")
                        if modulo_em_chamada == mod_alvo["id"]:
                            st.info("Exclusão bloqueada: este é o módulo da chamada aberta agora. Feche a chamada antes.")
                        elif n_pres_mod:
                            st.info(
                                f"Exclusão bloqueada: há {n_pres_mod} presença(s) registrada(s) neste módulo. "
                                "Apagar o módulo deixaria o histórico sem referência."
                            )
                        elif n_mat_mod:
                            st.warning(
                                f"Exclusão bloqueada: há {n_mat_mod} aluno(s) matriculado(s). "
                                "Remova as matrículas na aba 👥 Alunos e Matrículas."
                            )
                        else:
                            confirma_mod = st.checkbox(
                                f"Confirmo a exclusão do módulo {mod_alvo['numero']}",
                                key=f"chk_exclui_mod_{chave_edicao}",
                            )
                            if st.button(
                                "🗑️ Excluir módulo", type="primary",
                                disabled=not confirma_mod, key=f"btn_exclui_mod_{chave_edicao}",
                            ):
                                try:
                                    client.table("modulos").delete().eq("id", int(mod_alvo["id"])).execute()
                                    st.session_state["flash_modulos"] = f"🗑️ Módulo {mod_alvo['numero']} removido."
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível remover: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("remover o módulo")

        with tab4:
            try:
                alunos_base = client.table("alunos").select("id, nome").order("nome").execute().data or []
                vinculos = client.table("matriculas").select("id, aluno_id, modulo_id").execute().data or []
                registros_presenca = client.table("presenca").select("aluno_id").execute().data or []
            except APIError as erro:
                st.error(f"Não foi possível carregar a base de alunos: {erro.message}")
                st.stop()
            except ERROS_CONEXAO:
                avisar_conexao("carregar a base de alunos")
                st.stop()

            qtd_matriculas = Counter(v["aluno_id"] for v in vinculos)
            qtd_presencas = Counter(r["aluno_id"] for r in registros_presenca)
            nomes_cadastrados = {normalizar(a["nome"]).lower(): a["nome"] for a in alunos_base}
            mapa_modulos = {m["id"]: f"Mód {m['numero']} - {m['nome']}" for m in (res_modulos.data or [])}

            if st.session_state.get("flash_alunos"):
                st.success(st.session_state.pop("flash_alunos"))

            st.subheader("1. Base Geral: Cadastro Permanente de Alunos")
            st.caption(f"{len(alunos_base)} pessoa(s) na base do CCM.")

            aba_consultar, aba_cadastrar, aba_manter = st.tabs(
                ["📋 Consultar", "➕ Cadastrar", "✏️ Corrigir / Remover"]
            )

            with aba_consultar:
                if not alunos_base:
                    st.info("A base ainda está vazia. Use a aba **Cadastrar** para começar.")
                else:
                    termo = normalizar(st.text_input(
                        "🔎 Buscar por nome",
                        placeholder="Digite parte do nome...",
                        key="busca_base_alunos",
                    )).lower()
                    filtrados = [a for a in alunos_base if termo in a["nome"].lower()] if termo else alunos_base

                    if filtrados:
                        df_base_alunos = pd.DataFrame([
                            {
                                "Aluno": a["nome"],
                                "Matrículas": qtd_matriculas.get(a["id"], 0),
                                "Presenças": qtd_presencas.get(a["id"], 0),
                            }
                            for a in filtrados
                        ])
                        df_base_alunos.index = range(1, len(df_base_alunos) + 1)
                        st.dataframe(df_base_alunos, width='stretch', height=400)
                        st.caption(f"Exibindo {len(filtrados)} de {len(alunos_base)} cadastros.")

                        st.download_button(
                            "📥 Baixar base em CSV",
                            data=df_base_alunos.to_csv(index=False).encode("utf-8-sig"),
                            file_name="base_alunos_ccm.csv",
                            mime="text/csv",
                            key="download_base_alunos",
                        )

                        sem_vinculo = sorted(
                            a["nome"] for a in alunos_base if qtd_matriculas.get(a["id"], 0) == 0
                        )
                        if sem_vinculo:
                            with st.expander(f"⚠️ {len(sem_vinculo)} cadastro(s) sem nenhuma matrícula"):
                                st.caption(
                                    "Estas pessoas estão na base, mas não aparecem em nenhuma lista de "
                                    "chamada — falta matriculá-las em um módulo (seção 2)."
                                )
                                st.write(" · ".join(sem_vinculo))
                    else:
                        st.info("Nenhum cadastro encontrado com esse termo.")

            with aba_cadastrar:
                with st.form("form_novo_aluno", clear_on_submit=True):
                    nome_digitado = st.text_input("Nome Completo")
                    salvar_aluno = st.form_submit_button("Salvar Cadastro na Base", type="primary")

                if salvar_aluno:
                    nome_limpo = normalizar(nome_digitado)
                    if not nome_limpo:
                        st.warning("Informe o nome antes de salvar.")
                    elif nome_limpo.lower() in nomes_cadastrados:
                        st.error(f"'{nome_limpo}' já está cadastrado na base.")
                    else:
                        parecidos = difflib.get_close_matches(
                            nome_limpo.lower(), list(nomes_cadastrados.keys()), n=3, cutoff=0.8
                        )
                        if parecidos:
                            st.session_state["aluno_pendente"] = nome_limpo
                            st.session_state["aluno_parecidos"] = [nomes_cadastrados[p] for p in parecidos]
                        else:
                            cadastrar_aluno(nome_limpo)

                nome_pendente = st.session_state.get("aluno_pendente")
                if nome_pendente:
                    with st.container(border=True):
                        st.warning(
                            "Já existe cadastro parecido: "
                            + " · ".join(st.session_state.get("aluno_parecidos", []))
                        )
                        st.markdown(f"Confirme se **{nome_pendente}** é mesmo outra pessoa.")
                        col_sim, col_nao = st.columns(2)
                        if col_sim.button("Cadastrar mesmo assim", type="primary", width='stretch'):
                            cadastrar_aluno(nome_pendente)
                        if col_nao.button("Cancelar", width='stretch'):
                            st.session_state.pop("aluno_pendente", None)
                            st.session_state.pop("aluno_parecidos", None)
                            st.rerun()

            with aba_manter:
                if not alunos_base:
                    st.info("Nenhum cadastro para corrigir ainda.")
                else:
                    escolhido = st.selectbox(
                        "Selecione o cadastro",
                        options=[a["nome"] for a in alunos_base],
                        index=None,
                        placeholder="Digite para buscar...",
                        key="sel_manter_aluno",
                    )

                    if escolhido:
                        aluno_alvo = next(a for a in alunos_base if a["nome"] == escolhido)
                        n_matriculas = qtd_matriculas.get(aluno_alvo["id"], 0)
                        n_presencas = qtd_presencas.get(aluno_alvo["id"], 0)
                        modulos_do_aluno = sorted(
                            mapa_modulos.get(v["modulo_id"], "Módulo removido")
                            for v in vinculos if v["aluno_id"] == aluno_alvo["id"]
                        )

                        with st.container(border=True):
                            st.markdown(f"**{aluno_alvo['nome']}**")
                            st.caption(f"{n_matriculas} matrícula(s) · {n_presencas} presença(s) no histórico")
                            if modulos_do_aluno:
                                st.caption("Matriculado em: " + " · ".join(modulos_do_aluno))

                        st.markdown("**Corrigir nome**")
                        nome_corrigido = st.text_input(
                            "Nome corrigido", value=aluno_alvo["nome"], key=f"txt_corrige_nome_{aluno_alvo['id']}"
                        )
                        if st.button("Salvar correção", key=f"btn_corrige_nome_{aluno_alvo['id']}"):
                            novo = normalizar(nome_corrigido)
                            if not novo:
                                st.warning("O nome não pode ficar vazio.")
                            elif novo == aluno_alvo["nome"]:
                                st.info("Nada foi alterado.")
                            elif novo.lower() in nomes_cadastrados:
                                st.error("Já existe outro cadastro com esse nome.")
                            else:
                                try:
                                    client.table("alunos").update({"nome": novo}).eq(
                                        "id", int(aluno_alvo["id"])
                                    ).execute()
                                    st.session_state["flash_alunos"] = f"✅ Nome corrigido para {novo}."
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível corrigir: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("corrigir o nome")
                        st.caption(
                            "A correção reflete automaticamente nas listas de chamada e nos relatórios: "
                            "o histórico é vinculado ao cadastro, não ao texto do nome."
                        )

                        st.write("---")
                        st.markdown("**Remover da base**")
                        if n_presencas > 0:
                            st.info(
                                f"Remoção bloqueada: há {n_presencas} presença(s) registrada(s) para esta "
                                "pessoa. Excluir o cadastro deixaria buracos no diário de classe. Se ela saiu "
                                "do curso, remova apenas a matrícula do módulo (seção 3)."
                            )
                        elif n_matriculas > 0:
                            st.warning(
                                f"Remoção bloqueada: há {n_matriculas} matrícula(s) ativa(s). Desvincule na "
                                "seção 3 antes de excluir da base."
                            )
                        else:
                            confirmado = st.checkbox(
                                f"Confirmo a exclusão permanente de {aluno_alvo['nome']}",
                                key=f"chk_exclui_aluno_{aluno_alvo['id']}",
                            )
                            if st.button(
                                "🗑️ Excluir cadastro", type="primary",
                                disabled=not confirmado, key=f"btn_exclui_aluno_{aluno_alvo['id']}",
                            ):
                                try:
                                    client.table("alunos").delete().eq("id", int(aluno_alvo["id"])).execute()
                                    st.session_state["flash_alunos"] = f"🗑️ {aluno_alvo['nome']} removido da base."
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível remover: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("remover o cadastro")

            st.write("---")
            st.subheader("2. Efetivar Nova Matrícula em um Módulo")
            if alunos_base and res_modulos.data:
                dict_todos_alunos = {aluno['nome']: aluno['id'] for aluno in alunos_base}
                aluno_para_matricular = st.selectbox("Selecione a pessoa:", options=list(dict_todos_alunos.keys()))
                dict_modulos_mat = {f"Mód {m['numero']} - {m['nome']}": m['id'] for m in res_modulos.data}
                modulo_para_matricular_texto = st.selectbox("Selecione o Módulo de Destino:", options=list(dict_modulos_mat.keys()))
                id_mod_mat = dict_modulos_mat[modulo_para_matricular_texto]

                if st.button("Efetivar Matrícula no Módulo"):
                    try:
                        client.table("matriculas").insert({"aluno_id": int(dict_todos_alunos[aluno_para_matricular]), "modulo_id": int(id_mod_mat)}).execute()
                        st.success("Matrícula vinculada com sucesso!")
                        st.rerun()
                    except APIError as erro:
                        if duplicado(erro):
                            st.warning("Aluno já matriculado neste módulo.")
                        else:
                            st.error(f"Não foi possível matricular: {erro.message}")
                    except ERROS_CONEXAO:
                        avisar_conexao("efetivar a matrícula")
            else:
                st.info("Cadastre alunos e módulos antes de efetivar matrículas.")

            st.write("---")
            st.subheader("3. Verificar Alunos Matriculados por Módulo")
            if res_modulos.data:
                dict_ver_matriculas = {f"Mód {m['numero']} - {m['nome']}": m['id'] for m in res_modulos.data}
                mod_escolhido_ver = st.selectbox("Selecione o Módulo para ver os alunos ativos:", options=list(dict_ver_matriculas.keys()), key="sb_ver_mat")
                id_mod_ver = dict_ver_matriculas[mod_escolhido_ver]

                try:
                    res_lista_mat_filtrada = client.table("matriculas").select("id, alunos(id, nome)").eq("modulo_id", id_mod_ver).execute()
                except ERROS_CONEXAO:
                    avisar_conexao("listar as matrículas")
                    st.stop()

                if res_lista_mat_filtrada.data:
                    matriculados = sorted(
                        (
                            {"id_matricula": m["id"], "nome": m["alunos"]["nome"]}
                            for m in res_lista_mat_filtrada.data if m["alunos"]
                        ),
                        key=lambda x: x["nome"],
                    )
                    df_alunos_mat = pd.DataFrame([{"Nome do Aluno Ativo": m["nome"]} for m in matriculados])
                    df_alunos_mat.index = range(1, len(df_alunos_mat) + 1)

                    st.write("")
                    with st.container(border=True):
                        st.markdown(f"📊 **Total de alunos matriculados nesta matéria:** `{len(df_alunos_mat)}`")
                        st.write("")
                        st.dataframe(df_alunos_mat, width='stretch', height=400)

                    with st.expander("➖ Remover matrícula deste módulo"):
                        mapa_matriculas = {m["nome"]: m["id_matricula"] for m in matriculados}
                        alvo_desvincular = st.selectbox(
                            "Aluno a desvincular",
                            options=list(mapa_matriculas.keys()),
                            index=None,
                            placeholder="Selecione...",
                            key="sel_remove_matricula",
                        )
                        if alvo_desvincular:
                            st.caption(
                                "As presenças já registradas continuam no histórico. O aluno apenas deixa "
                                "de constar na lista de chamada e no relatório deste módulo."
                            )
                            if st.button("Remover matrícula", key="btn_remove_matricula"):
                                try:
                                    client.table("matriculas").delete().eq(
                                        "id", int(mapa_matriculas[alvo_desvincular])
                                    ).execute()
                                    st.session_state["flash_alunos"] = f"➖ {alvo_desvincular} desvinculado do módulo."
                                    st.rerun()
                                except APIError as erro:
                                    st.error(f"Não foi possível remover a matrícula: {erro.message}")
                                except ERROS_CONEXAO:
                                    avisar_conexao("remover a matrícula")
                else:
                    st.info("Nenhum aluno matriculado especificamente neste módulo ainda.")
            else:
                st.info("Cadastre módulos primeiro.")

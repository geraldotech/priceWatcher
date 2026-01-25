import schedule
import time
import requests
import os
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime, date
import threading
import queue
import re
from bs4 import BeautifulSoup
import smtplib
import ssl
import configparser
from email.message import EmailMessage
from decimal import Decimal, InvalidOperation


# ---------- CONFIGURAÇÃO DE LOGS ----------

outLogDir = 'logs/'
os.makedirs(outLogDir, exist_ok=True)  # cria a pasta de logs

# Logger geral (info.log)
info_logger = logging.getLogger('info_logger')
info_logger.setLevel(logging.INFO)
info_handler = logging.FileHandler(os.path.join(outLogDir, 'info.log'), encoding='utf-8')
info_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                            datefmt='%Y-%m-%d %H:%M:%S'))
info_logger.addHandler(info_handler)
info_logger.propagate = False

# Logger de sucesso (log_success.log)
success_logger = logging.getLogger('success_logger')
success_logger.setLevel(logging.INFO)
success_handler = logging.FileHandler(os.path.join(outLogDir, 'log_success.log'), encoding='utf-8')
success_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                               datefmt='%Y-%m-%d %H:%M:%S'))
success_logger.addHandler(success_handler)
success_logger.propagate = False

# Logger de erros (log_erros.log)
error_logger = logging.getLogger('error_logger')
error_logger.setLevel(logging.ERROR)
error_handler = logging.FileHandler(os.path.join(outLogDir, 'log_erros.log'), encoding='utf-8')
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s',
                                             datefmt='%Y-%m-%d %H:%M:%S'))
error_logger.addHandler(error_handler)
error_logger.propagate = False

# ---------- CONFIG GLOBAL ----------

config = None
ultima_modificacao = None
config_path = Path('db') / 'config.json'

task_queue = queue.Queue()
WORKER_DELAY_SECONDS = 2  # delay entre execuções para não "explodir" requisições

email_config = {}
email_path = Path("db") / "email.ini"
sites_rules = {}
# tenta primeiro em db/sites.json, cai para sites.json na raiz
sites_path_candidates = [Path('db') / 'sites.json', Path('sites.json')]
sites_path = sites_path_candidates[0]

# ---------- FUNÇÕES UTIL ----------

def carregar_email_config():
    global email_config
    try:
        parser = configparser.ConfigParser()
        parser.read(email_path, encoding="utf-8")

        if "email" not in parser:
            raise ValueError("Seção [email] não encontrada")

        sec = parser["email"]

        destinatarios_raw = sec.get("destinatarios", "")
        destinatarios = [
            e.strip() for e in destinatarios_raw.split(",") if e.strip()
        ]

        email_config = {
            "email": sec.get("email"),
            "senha": sec.get("senha"),
            "remetente": sec.get("remetente"),
            "smtp": sec.get("smtp"),
            "porta": sec.getint("portaSmtp", 465),
            "auth": sec.getboolean("autenticacao", True),
            "destinatarios": destinatarios
        }

        info_logger.info(
            f"Configuração de email carregada | destinatarios={destinatarios}"
        )
        return True

    except Exception as e:
        email_config = {}
        error_logger.error(f"Erro ao carregar email.ini: {e}")
        return False

    
def parse_price_text(valor: str) -> Decimal | None:
    """Converte texto BR vindo do site (ex: 'R$ 6.020,90') em Decimal('6020.90')."""
    if not valor:
        return None
    try:
        s = str(valor).replace("R$", "").replace("\xa0", " ").strip()
        s = re.sub(r"[^\d\.,]", "", s)   # mantém só dígitos e separadores

        # remove milhar com ponto
        s = s.replace(".", "")

        # troca decimal (,) por ponto
        s = s.replace(",", ".")

        if not s or s == ".":
            return None

        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_target(value) -> Decimal | None:
    """Converte targetPrice do JSON (número 15.98) em Decimal('15.98')."""
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def fmt_brl_decimal(d: Decimal | None) -> str:
    """Formata Decimal em BR: R$ 12.000,00. Se None, retorna '—'."""
    if d is None:
        return "—"
    s = f"{d:,.2f}"  # ex: 12,000.00
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")  # 12.000,00
    return "R$ " + s



def fetch_amazon_price(url: str, timeout: int = 15) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()

    html = r.content.decode("utf-8-sig", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    # 1) ✅ mais confiável: priceToPay (reinvent)
    price_to_pay = soup.select_one(
        "span.a-price.reinventPricePriceToPayMargin.priceToPay span.a-offscreen"
    )
    if price_to_pay:
        txt = price_to_pay.get_text(" ", strip=True)
        txt = normalizar_preco_br(txt)
        if txt:
            return txt

    # 2) ✅ fallback: tenta outros comuns (opcional, mas ajuda)
    for sel in [
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        "span.a-price span.a-offscreen",
    ]:
        el = soup.select_one(sel)
        if el:
            txt = normalizar_preco_br(el.get_text(" ", strip=True))
            if txt:
                return txt

    # 3) fallback antigo (seu método atual)
    whole = soup.select_one(".a-price-whole")
    frac = soup.select_one(".a-price-fraction")
    sym = soup.select_one(".a-price-symbol")

    if not whole:
        return None

    whole_txt = re.sub(r"[^\d\.]", "", whole.get_text(" ", strip=True))
    frac_txt = re.sub(r"[^\d]", "", frac.get_text(" ", strip=True) if frac else "00") or "00"
    symbol = sym.get_text(strip=True) if sym else "R$"

    debug_path = Path("logs") / "debug_amazon.html"
    debug_path.write_text(html, encoding="utf-8", errors="ignore")
    info_logger.info(f"Amazon HTML salvo em {debug_path} | final_url={r.url}")


    return f"{symbol} {whole_txt},{frac_txt}"



def enviar_email(assunto: str, corpo_html: str, corpo_texto: str | None = None, destinatarios: list[str] | None = None) -> bool:
    if not email_config:
        error_logger.error("Config de email não carregada.")
        return False

    if not destinatarios:
        destinatarios = email_config.get("destinatarios", [])

    if not destinatarios:
        error_logger.error("Nenhum destinatário configurado para envio de email.")
        return False

    try:
        msg = EmailMessage()
        msg["From"] = email_config["remetente"]
        msg["To"] = ", ".join(destinatarios)
        msg["Subject"] = assunto

        # fallback texto (bom para entregabilidade)
        if not corpo_texto:
            corpo_texto = re.sub(r"<[^>]+>", "", corpo_html)  # remove tags simples

        msg.set_content(corpo_texto)
        msg.add_alternative(corpo_html, subtype="html")

        context = ssl.create_default_context()

        with smtplib.SMTP_SSL(email_config["smtp"], email_config["porta"], context=context) as server:
            if email_config["auth"]:
                server.login(email_config["email"], email_config["senha"])
            server.send_message(msg)

        success_logger.info(f"E-mail enviado | para={destinatarios} | assunto='{assunto}'")
        return True

    except Exception as e:
        error_logger.error(f"Erro ao enviar email: {e}")
        return False

def montar_email_html(
    titulo: str,
    produto: str,
    url: str,
    preco_atual: str,
    preco_alvo: str = "—",
    site: str = "",
    quando: str = ""
) -> str:
    quando = quando or datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # CSS inline (melhor suporte em email)
    return f"""
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f6f7fb;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7fb;padding:24px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 6px 22px rgba(0,0,0,.08);">
            <!-- Header -->
            <tr>
              <td style="padding:18px 22px;background:#111827;color:#ffffff;">
                <div style="font-size:14px;opacity:.9;">PriceWatcher</div>
                <div style="font-size:20px;font-weight:700;line-height:1.2;margin-top:4px;">{titulo}</div>
              </td>
            </tr>

            <!-- Body -->
            <tr>
              <td style="padding:22px;">
                <div style="font-size:16px;font-weight:700;color:#111827;margin-bottom:8px;">{produto}</div>

                <div style="font-size:13px;color:#6b7280;margin-bottom:14px;">
                  {site} • {quando}
                </div>

                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:12px;">
                  <tr>
                    <td style="padding:14px 16px;border-bottom:1px solid #e5e7eb;">
                      <div style="font-size:12px;color:#6b7280;">Preço atual</div>
                      <div style="font-size:22px;font-weight:800;color:#111827;margin-top:2px;">{preco_atual}</div>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:14px 16px;">
                      <div style="font-size:12px;color:#6b7280;">Preço alvo</div>
                      <div style="font-size:16px;font-weight:700;color:#111827;margin-top:2px;">{preco_alvo}</div>
                    </td>
                  </tr>
                </table>

                <div style="margin-top:16px;font-size:13px;color:#6b7280;line-height:1.5;">
                  Abrir o produto:
                </div>

                <div style="margin-top:10px;">
                  <a href="{url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 16px;border-radius:10px;font-weight:700;font-size:14px;">
                    Ver produto
                  </a>
                </div>

                <div style="margin-top:18px;font-size:12px;color:#9ca3af;line-height:1.5;">
                  Dica: você pode configurar <b>targetPrice</b> e o controle anti-spam (<b>alertSent</b>) no <code>db/config.json</code>.
                </div>
              </td>
            </tr>

            <!-- Footer -->
            <tr>
              <td style="padding:14px 22px;background:#f3f4f6;color:#6b7280;font-size:12px;">
                Enviado automaticamente pelo PriceWatcher.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
""".strip()



def carregar_sites():
    """Carrega regras de seletores de preço (primeiro db/sites.json, depois sites.json)."""
    global sites_rules, sites_path
    for path in sites_path_candidates:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                sites_rules = json.load(f)
                sites_path = path
                info_logger.info(f"Arquivo sites.json carregado com sucesso ({path}).")
                return True
        except FileNotFoundError:
            # tenta próximo caminho
            continue
        except json.JSONDecodeError:
            info_logger.error(f"Erro ao decodificar o arquivo sites.json ({path}).")
            sites_rules = {}
            return False
        except Exception as e:
            info_logger.error(f"Erro inesperado ao carregar sites.json ({path}): {e}")
            sites_rules = {}
            return False

    info_logger.error("Arquivo sites.json não encontrado em db/sites.json nem sites.json.")
    sites_rules = {}
    return False

def extrair_dominio(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.replace("www.", "")

def fetch_selector_text(url: str, selector: str, timeout: int = 15) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
    }

    with requests.Session() as s:
        r = s.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()

    html = r.content.decode("utf-8-sig", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    el = soup.select_one(selector)
    if not el:
        # salva pra você abrir e ver o que veio de verdade
        debug_path = Path("logs") / "debug_last_page.html"
        debug_path.write_text(html, encoding="utf-8", errors="ignore")

        title = soup.title.get_text(strip=True) if soup.title else "(sem title)"
        error_logger.error(f"Selector não encontrado. final_url={r.url} title={title} salvo_em={debug_path}")
        return None

    raw = el.get_text(" ", strip=True)
    return normalizar_preco_br(raw)


def normalizar_preco_br(txt: str) -> str:
    txt = normalizar_texto(txt)

    # remove espaços ao redor de vírgula entre dígitos: "15 , 98" -> "15,98"
    txt = re.sub(r"(\d)\s*,\s*(\d)", r"\1,\2", txt)

    # remove espaços ao redor de ponto entre dígitos (caso apareça): "1 . 234" -> "1.234"
    txt = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", txt)

    # se tiver "R$15,98" -> "R$ 15,98"
    txt = re.sub(r"^R\$\s*", "R$ ", txt)

    return txt

def normalizar_texto(txt: str) -> str:
    if txt is None:
        return ""

    # remove BOM e chars invisíveis comuns
    txt = txt.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")

    # converte NBSP e variações pra espaço normal
    txt = txt.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ")

    # colapsa qualquer sequência de whitespace em 1 espaço e trim
    txt = re.sub(r"\s+", " ", txt, flags=re.UNICODE).strip()

    return txt


def buscar_preco_por_site(url: str) -> dict:
    dominio = extrair_dominio(url)

    # tratamento para Amazon
    if "amazon." in dominio:
        try:
            text = fetch_amazon_price(url)
            if not text:
                return {"ok": False, "dominio": dominio, "selector": "amazon-special", "value": None,
                        "error": "Preço Amazon não encontrado (.a-price-whole)"}
            return {"ok": True, "dominio": dominio, "selector": "amazon-special", "value": normalizar_preco_br(text), "error": None}
        except Exception as e:
            return {"ok": False, "dominio": dominio, "selector": "amazon-special", "value": None, "error": str(e)}

    # resto dos sites
    regra = sites_rules.get(dominio)
    if not regra or "price" not in regra or "selector" not in regra["price"]:
        return {"ok": False, "dominio": dominio, "selector": None, "value": None,
                "error": f"Sem regra de selector em sites.json para {dominio}"}

    selector = regra["price"]["selector"]

    try:
        text = fetch_selector_text(url, selector)
        if text is None:
            return {"ok": False, "dominio": dominio, "selector": selector, "value": None,
                    "error": f"Selector não encontrado: {selector}"}
        return {"ok": True, "dominio": dominio, "selector": selector, "value": text, "error": None}
    except Exception as e:
        return {"ok": False, "dominio": dominio, "selector": selector, "value": None, "error": str(e)}



def _worker_loop():
    while True:
        task = task_queue.get()
        try:
            executar_tarefa_se_ativa(task)
            time.sleep(WORKER_DELAY_SECONDS)
        except Exception as e:
            error_logger.error(f"Worker error: {e}")
        finally:
            task_queue.task_done()

def iniciar_worker():
    t = threading.Thread(target=_worker_loop, daemon=True)
    t.start()
    info_logger.info("Worker de execução lenta iniciado.")
    return t

def enfileirar_task(task):
    """Job do schedule: só coloca na fila (execução real é no worker)."""
    # checa status em runtime (mesmo se mudou depois do agendamento)
    if not task.get("status", False):
        info_logger.info(f"Não enfileirada (desativada): {task.get('description','(sem descrição)')}")
        return
    task_queue.put(task)
    info_logger.info(f"Enfileirada: {task.get('description','(sem descrição)')}")

def salvar_config(data: dict) -> bool:
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        error_logger.error(f"Erro ao salvar config.json: {e}")
        return False

# ---------- I/O CONFIG ----------

def carregar_config():
    global config, ultima_modificacao
    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            config = json.load(file)
            info_logger.info("Arquivo config.json carregado com sucesso.")
            ultima_modificacao = os.path.getmtime(config_path)
            return True
    except FileNotFoundError:
        info_logger.error("Arquivo config.json não encontrado.")
        print(f"ERRO: Crie o arquivo {config_path} ou ajuste o caminho")
    except json.JSONDecodeError:
        info_logger.error("Erro ao decodificar o arquivo config.json.")
    except Exception as e:
        info_logger.error(f"Erro inesperado ao carregar config.json: {e}")
    return False

def verificar_modificacao():
    """Reload hot quando o arquivo mudar: recarrega config e reagenda tudo."""
    global config, ultima_modificacao
    try:
        mod_time = os.path.getmtime(config_path)
        if ultima_modificacao is None or mod_time != ultima_modificacao:
            info_logger.info("Recarregando agendamentos após alteração do config.json...")
            print("\n Configuração modificada - Recarregando...")
            if carregar_config():
                schedule.clear()
                agendar_requisicoes()
                mostrar_agendamentos()
            else:
                print("Erro ao carregar config, mantendo agendamentos atuais.")
    except Exception as e:
        error_logger.error(f"Erro ao verificar modificações: {str(e)}")

# ---------- EXECUÇÃO DAS TAREFAS ----------
def executar_tarefa_se_ativa(task):
    """Executa se a task continuar com status=True (checa em runtime)."""
    try:
        if not task.get("status", False):
            info_logger.info(f"Tarefa cancelada: {task.get('description','(sem descrição)')} (status alterado)")
            return

        url = task["url"]
        desc = task.get("description", "(sem descrição)")

        start_time = time.time()
        resp = buscar_preco_por_site(url)
        elapsed = round(time.time() - start_time, 2)

        parsed = urlparse(url)
        short_url = f"{parsed.netloc}{parsed.path}"

        if resp.get("ok"):
            valor = resp["value"]
            task["lastValue"] = valor

            # LOG SUCESSO SEMPRE (captura OK)
            success_logger.info(
                f"{desc} | {short_url} | selector={resp.get('selector')} | value={valor} | tempo de busca={elapsed:.2f}s"
            )

            # parse correto
            preco_atual_dec = parse_price_text(valor)                 # Decimal ou None
            target_dec = parse_target(task.get("targetPrice"))        # Decimal ou None

            # formata bonito no email
            preco_atual_fmt = fmt_brl_decimal(preco_atual_dec) if preco_atual_dec is not None else valor
            preco_alvo_fmt  = fmt_brl_decimal(target_dec)

            html = montar_email_html(
                titulo="🔥 Alerta de preço",
                produto=desc,
                url=task.get("url"),
                preco_atual=preco_atual_fmt,
                preco_alvo=preco_alvo_fmt,
                site=extrair_dominio(task.get("url")),
            )

            texto = (
                f"{desc}\n"
                f"{task.get('url')}\n\n"
                f"Preço atual: {preco_atual_fmt}\n"
                f"Preço alvo: {preco_alvo_fmt}\n"
                f"Data/Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            )

            # -------- decide 1x --------
            should_send = False    
            reason = ''        

            # 1) sendNow tem prioridade
            if task.get("sendNow", False):
                should_send = True                
                reason = "Envio de email imediato | sendNow detectado"
                # auto-reset pra não virar spam mudar o valor do sendNow (entao o email so é disparado uma vez)
                #task["sendNow"] = False

            else:
                # 2) alerta por targetPrice (anti-spam com alertSent)
                if preco_atual_dec is not None and target_dec is not None:
                    if preco_atual_dec <= target_dec:
                        success_logger.info(f"preco_atual_dec <= target_dec | {desc}")
                        if task.get("alertSent", True):
                            should_send = True
                            reason = "PRECO BAIXO CORRE!"
                            task["alertSent"] = False # reseta para nao enviar mais emails (antispam)
                        else:
                            success_logger.info(f"Alerta já disparado | nenhum email será enviado {desc}")
                    else:
                        task["alertSent"] = True  # rearma quando voltar a ficar acima
                        success_logger.info(f"{desc} - preco {preco_atual_dec}  maior do que o target {target_dec}")

            # envia UMA vez
            if should_send:
                enviar_email(
                    assunto=f"🔥 Alerta de preço: {desc}",
                    corpo_html=html,
                    corpo_texto=texto
                )
                success_logger.info(f"{desc} | email enviado | {reason}")

        # marca lastRun + salva
        try:
            #success_logger.info(f"marcando lastrun | {desc}")
            task["lastRun"] = datetime.now().isoformat(timespec="seconds")
            salvar_config(config)
        except Exception as e:
            error_logger.error(f"Erro ao salvar config.json (lastRun/alertSent/sendNow): {e}")

    except Exception as e:
        error_msg = f"{task.get('description','(sem descrição)')} | Erro: {str(e)}"
        print(error_msg)
        error_logger.error(error_msg)



def agendar_requisicoes():
    if not config or 'tasks' not in config:
        info_logger.error("Erro: Configuração inválida ou sem tarefas!")
        return

    for task in config['tasks']:

        # aceita novo padrão: times[]
        times = task.get("times")

        # compat: se ainda vier "time", converte para times
        if not times and task.get("time"):
            times = [task["time"]]
            task["times"] = times

        required_fields = ['description', 'url', 'status', 'times', 'id']
        if not all(field in task for field in required_fields):
            info_logger.warning(f"Tarefa incompleta: {task.get('url', 'URL não especificada')}")
            continue

        if not isinstance(times, list) or not times:
            info_logger.warning(f"'times' inválido/vazio na tarefa: {task.get('description','(sem descrição)')}")
            continue

        if not task.get('status', False):
            info_logger.info(f"Tarefa desativada: {task['description']}")
            continue

        # agenda cada horário (execução lenta via fila)
        for hhmm in times:
            try:
                schedule.every().day.at(hhmm).do(enfileirar_task, task=task)
                print(f"🔁 {hhmm} | {task['description']} | diário | Agendada (fila)")
                info_logger.info(f"Agendada (fila): {task['description']} ({hhmm})")
            except schedule.ScheduleValueError:
                info_logger.error(f"Hora inválida '{hhmm}' na tarefa: {task['description']}")


def mostrar_agendamentos():
    print("\n📅 Tarefas Agendadas:")
    print("HORÁRIOS                 | DESCRIÇÃO       | STATUS")
    print("-" * 70)

    for task in config.get('tasks', []):
        status2 = "🟢" if task.get('status', False) else "🔴"

        times = task.get("times")
        if not times and task.get("time"):
            times = [task["time"]]

        times_show = ", ".join(times) if isinstance(times, list) and times else "—"

        linha = f"{times_show:<22} | {task.get('description','')[:25]:<15} | {status2}"
        print(linha)
        info_logger.info(linha)


# ---------- MAIN LOOP ----------
if __name__ == "__main__":
    print("Script iniciado...")

    intervalo_verificacao = 5

    if not carregar_config():
        print("Falha ao carregar config.json!")
        exit(1)

    carregar_sites()
    carregar_email_config()   # <-- antes do worker

    iniciar_worker()
    mostrar_agendamentos()
    agendar_requisicoes()

    try:
        last_check = time.time()
        while True:
            # hot reload do config.json
            if time.time() - last_check >= intervalo_verificacao:
                verificar_modificacao()
                last_check = time.time()

            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        info_logger.info("Script interrompido pelo usuário.")
        print("\n Script finalizado pelo usuário")

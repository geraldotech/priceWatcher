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
from http.server import BaseHTTPRequestHandler, HTTPServer
import queue


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

# ---------- FUNÇÕES UTIL ----------


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

def _parse_date(dstr: str | None) -> date | None:
    """Aceita 'YYYY/MM/DD' ou 'YYYY-MM-DD'."""
    if not dstr:
        return None
    d = dstr.strip().replace("-", "/")
    try:
        y, m, d = map(int, d.split("/"))
        return date(y, m, d)
    except Exception:
        info_logger.warning(f"Data inválida em task: '{dstr}' (esperado YYYY/MM/DD ou YYYY-MM-DD)")
        return None

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

# ---------- HTTP ----------

def fazer_get(url, description):
    result = {"status": None, "tempo": None, "conteudo": None}

    start_time = time.time()
    conteudo = None  
    
    try:
        parsed = urlparse(url)
        short_url = f"{parsed.netloc}{parsed.path}" if parsed.netloc else (url if len(url) <= 30 else url[:30] + '...')
        resposta = requests.get(url, timeout=10)
        elapsed = time.time() - start_time

        print(f"✅ {description} | Status: {resposta.status_code} | Tempo: {elapsed:.2f}s")
        info_logger.info(f"Sucesso na chamada de: {short_url}")

        result["status"] = resposta.status_code
        result["tempo"] = elapsed

        if resposta.status_code == 200:
            result["conteudo"] = conteudo
            conteudo = resposta.content.decode('utf-8-sig', errors='replace').strip()
            if not conteudo:
                conteudo = "[resposta vazia ou ilegível]"
            success_logger.info(f"Response from: {short_url}: {conteudo}")
            print(f"✅ Sucesso 200 {description} [{time.strftime('%H:%M')}]")
        else:
            msg = f"⚠️ Falha: {short_url} → Status {resposta.status_code}"
            info_logger.warning(msg)
            error_logger.error(msg)
            print(f"⚠️ [{time.strftime('%H:%M')}] {short_url} → Status {resposta.status_code}")

        return result

    except requests.exceptions.RequestException as e: 
        # calcula tempo no timeout
        elapsed = round(time.time() - start_time, 2)  
        result["tempo"] = elapsed
        result["status"] = "ERROR"
        result["conteudo"] = str(e)

        error_logger.error(f"Erro na requisição para {url}: {str(e)} (tempo: {elapsed}s)")
        print(f"Erro [{time.strftime('%H:%M')}] {url} → {str(e)}")

        return result


# ---------- EXECUÇÃO DAS TAREFAS ----------

def executar_tarefa_se_ativa(task):
    """Executa se a task continuar com status=True (checa em runtime)."""
    try:
        if not task.get('status', False):
            info_logger.info(f"Tarefa cancelada: {task.get('description', '(sem descrição)')} (status alterado)")
            return
        fazer_get(task['url'], task['description'])
        # Marca lastRun (opcional)
        try:
            task["lastRun"] = datetime.now().isoformat()
            salvar_config(config)
        except Exception:
            pass
    except Exception as e:
        error_msg = f"{task.get('description','(sem descrição)')} | Erro: {str(e)}"
        print(error_msg)
        error_logger.error(error_msg)

def executar_unico_no_dia(task, data_alvo_str: str):
    """Executa a tarefa apenas no dia alvo na time programada e depois cancela o job."""
    alvo = _parse_date(data_alvo_str)
    hoje = date.today()
    if not alvo:
        # Se a data estiver inválida, cancela
        info_logger.warning(f"Tarefa com data inválida cancelada: {task.get('description','(sem descrição)')}")
        return schedule.CancelJob

    if hoje < alvo:
        # Ainda não é o dia → não roda (mantém agendado para verificar novamente no próximo 'at')
        return

    if hoje == alvo:
        # É o dia → executa e cancela
        executar_tarefa_se_ativa(task)
        return schedule.CancelJob

    if hoje > alvo:
        # Passou do dia → cancela
        info_logger.info(f"Tarefa expirada cancelada: {task.get('description','(sem descrição)')} ({data_alvo_str})")
        return schedule.CancelJob

# ---------- AGENDA ----------
iniciar_worker()

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

        linha = f"{times_show:<22} | {task.get('description','')[:15]:<15} | {status2}"
        print(linha)
        info_logger.info(linha)


# ---------- MAIN LOOP ----------

class _HealthHandler(BaseHTTPRequestHandler):
    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

        # -----------------------------
    # Helpers internos
    # -----------------------------
    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status_code: int, data: dict):
        payload = json.dumps(data).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._set_cors()
        self.end_headers()

        self.wfile.write(payload)

    # -----------------------------
    # Handler principal
    # -----------------------------
    def do_GET(self):
        global config
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query or "")

        # =============================================
        # ENDPOINT: /run?id=XYZ  → execução manual
        # =============================================
        if path == "/run":
            raw_id = qs.get("id", [""])[0]

            # Busca pelo campo id como string
            tasks = config.get("tasks", [])
            task = next((t for t in tasks if str(t.get("id")) == raw_id), None)

            if not task:
                return self._send_json(404, {
                    "ok": False,
                    "error": f"task id '{raw_id}' not found"
                })

            # Executa a tarefa
            try:
                url = task["url"]
                desc = task["description"]
                resposta = fazer_get(url, desc)

                # marca lastRun
                try:
                    task["lastRun"] = datetime.now().isoformat(timespec="seconds")
                    salvar_config(config)
                except Exception as e:
                    error_logger.error(f"Erro ao salvar lastRun: {e}")        
            
                return self._send_json(200, {
                "ok": True,
                "status": resposta.get("status"),
                "tempo": resposta.get("tempo"),
                "conteudo": resposta.get("conteudo")
                })

            except Exception as e:
                return self._send_json(500, {
                    "ok": False,
                    "error": str(e)
                })

        # =============================================
        # ENDPOINT: /tasks → lista tarefas
        # =============================================
        if path == "/tasks":
            return self._send_json(200, {
                "ok": True,
                "tasks": config.get("tasks", [])
            })

        # =============================================
        # ROOT / status do servico
        # =============================================
        return self._send_json(200, {"ok": True})


def iniciar_healthcheck(porta=5051):
    try:
        servidor = HTTPServer(("0.0.0.0", porta), _HealthHandler)
    except OSError as e:
        error_logger.error(f"Falha ao iniciar healthcheck na porta {porta}: {e}")
        return None

    thread = threading.Thread(target=servidor.serve_forever, daemon=True)
    thread.start()
    info_logger.info(f"Healthcheck rodando em 0.0.0.0:{porta}")
    return servidor

if __name__ == "__main__":
    print("Script iniciado...")

    intervalo_verificacao = 5  # segundos

    if not carregar_config():
        print("Falha ao carregar config.json!")
        exit(1)

    iniciar_healthcheck()
    iniciar_worker()          # <<< aqui
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
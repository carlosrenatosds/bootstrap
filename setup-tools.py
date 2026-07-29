#!/usr/bin/env python3
"""
setup-tools.py — Instalador/configurador multiplataforma (Linux e Windows 11)

Uso:
    python setup-tools.py syncthing
    python setup-tools.py tailscale
    python setup-tools.py all

"""

import argparse
import ctypes
import os
import platform
import shutil
import subprocess
import sys
import time
import json
import re
import tempfile
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"
_DEBUG = False
_SKIP_PAUSE = False
SCRIPT_PATH = os.path.realpath(__file__)


def _load_dotenv() -> dict[str, str]:
    """Carrega variáveis de um arquivo .env no mesmo diretório do script.

    Formato: KEY=VALUE (um por linha). Linhas com # são comentários.
    """
    env_path = Path(__file__).resolve().parent / ".setup-tools.env"
    if not env_path.is_file():
        return {}
    env: dict[str, str] = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


_env = _load_dotenv()
DEFAULT_SERVER_ID = "U4UC6VN-SGVG3QC-EBVLO5K-QMXSX3E-SFYHHFA-ZNWVSSU-W6PTASB-KZZZVQ5"
DEFAULT_FOLDER_ID = "5nft6-vx92m"
DEFAULT_TAILKEY = _env.get("TAILSCALE_KEY", "")

# ==========================================================================
# Utilitários compartilhados
# ==========================================================================

def run(cmd, check=False, capture=False, input_str=None):
    """Executa um comando exibindo o que está sendo rodado."""
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd, check=check, capture_output=capture, text=True, input=input_str,
    )
    if _DEBUG and capture:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    return result


def prompt_with_default(message: str, default: str) -> str:
    """Equivalente ao `read -p ... -i default` do bash."""
    value = input(f"{message} [{default}]: ").strip()
    return value if value else default


def pause(message: str = "Pressione Enter para continuar..."):
    input(message)


def sudo_prefix() -> list[str]:
    return [] if IS_WINDOWS else ["sudo"]


def _refresh_path_if_needed(binary: str):
    """No Windows, recarrega o PATH se o binário não for encontrado.

    O winget atualiza o PATH do sistema, mas não o do processo atual.
    Usa where.exe para localizar o binário recém-instalado e, como fallback,
    busca em diretórios comuns de instalação (Program Files, AppData).
    """
    if shutil.which(binary):
        return
    if not IS_WINDOWS:
        return

    exe = None

    # 1) Tenta where.exe (usa App Paths do registro + PATH atual)
    result = subprocess.run(
        ["where", binary], capture_output=True, text=True,
    )
    if result.returncode == 0:
        exe = result.stdout.strip().splitlines()[0]

    # 2) Fallback: busca em diretórios comuns de instalação
    if not exe:
        search_dirs = []
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var, "")
            if base:
                search_dirs.append(Path(base))
        # Também busca no USERPROFILE (winget instala alguns pacotes lá)
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            search_dirs.append(Path(userprofile) / "AppData" / "Local"
                              / "Microsoft" / "WinGet" / "Packages")

        for base in search_dirs:
            try:
                for match in base.glob(f"**/{binary}.exe"):
                    exe = str(match)
                    break
            except OSError:
                pass
            if exe:
                break

    if exe:
        parent = str(Path(exe).parent)
        if parent not in os.environ.get("PATH", ""):
            os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")
            print(f"  → {exe} adicionado ao PATH")


def sudo_write(path: str | Path, content: str):
    """Escreve conteúdo em um arquivo do sistema via sudo cp."""
    tmp = Path(tempfile.mktemp())
    tmp.write_text(content)
    run(sudo_prefix() + ["cp", str(tmp), str(path)])
    tmp.unlink()
    print(f"  → {path}")


def current_user() -> str | None:
    return os.environ.get("USER") or os.environ.get("LOGNAME")


# ---- Leitor de teclas raw (cross-platform) ----

def _get_key() -> str:
    """Lê uma tecla do terminal em modo raw.

    Retorna:
        '\x1b'       — ESC
        '\x1b[A'     — Seta para cima
        '\x1b[B'     — Seta para baixo
        '\r' ou '\n' — Enter
    """
    if IS_WINDOWS:
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            return "\xe0" + ch2.decode("latin-1", errors="replace")
        return ch.decode("utf-8", errors="replace")
    else:
        import termios as _termios
        import tty
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return ""
        old = _termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            key = sys.stdin.buffer.read(1)
            if key == b"\x1b":
                # Configura timeout entre bytes via VMIN/VTIME (100 ms)
                attr = _termios.tcgetattr(fd)
                attr[6][_termios.VMIN] = 0
                attr[6][_termios.VTIME] = 1
                _termios.tcsetattr(fd, _termios.TCSANOW, attr)
                extra = b""
                while True:
                    ch = sys.stdin.buffer.read(1)
                    if not ch:
                        break
                    extra += ch
                key += extra
            return key.decode("utf-8", errors="replace")
        finally:
            _termios.tcsetattr(fd, _termios.TCSADRAIN, old)


# ---- Menu interativo ----

# Itens do menu (nome_da_tarefa, descrição, linux_only, needs_admin)
_MENU_ITEMS: list[tuple[str, str, bool, bool]] = [
    ("packages", "Instalar pacotes básicos sugeridos", False, True),
    ("laptop", "Configurações para laptops", True, False),
    ("tailscale", "Instalar e configurar o Tailscale", False, True),
    ("aws-install", "Instalar o AWS CLI", False, True),
    ("aws", "Configurar credenciais AWS (access key, região)", False, False),
    ("aws-test", "Testar a configuração do AWS CLI", False, False),
    ("gtk", "Customizar o GNOME para melhorar o uso do Fish4", True, False),
    ("extensions", "Instalar extensões GNOME indicadas para o Fish4", True, False),
    ("extensions-enable", "Ativar as extensões GNOME indicadas", True, False),
    ("keyd", "Forçar ENTER e , no teclado numérico", True, False),
    ("cups", "Configuração do CUPS (retenção dos jobs)", True, False),
    ("cups-off", "Pausar as filas de impressão do CUPS", True, False),
    ("cups-on", "Liberar as filas de impressão do CUPS", True, False),
    ("printers-add", "Instalar as impressoras usadas no Fish4", True, False),
    ("printers-del", "Remover as impressoras usadas no Fish4", True, False),
    ("syncthing-install", "Instalar o Syncthing e iniciar o serviço", False, True),
    ("syncthing-setup", "Configurar o Syncthing (pasta BUILDS)", False, False),
]


def _enable_ansi_windows():
    """Habilita processamento ANSI no terminal do Windows 10+."""
    if not IS_WINDOWS:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_VT = 0x0004
        for std_handle in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(std_handle)
            if handle:
                mode = ctypes.c_uint32()
                kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass  # Fallback: terminal pode não suportar ANSI


def interactive_menu():
    """Menu interativo com navegação por setas (estilo whiptail)."""
    global _SKIP_PAUSE

    if not os.isatty(sys.stdin.fileno()):
        print("Terminal interativo necessário para o menu.", file=sys.stderr)
        sys.exit(1)

    _enable_ansi_windows()

    items = [(name, desc, needs_admin)
             for name, desc, linux_only, needs_admin in _MENU_ITEMS
             if not (IS_WINDOWS and linux_only)]
    items.append(("sair", "Sair do menu", False))
    current = 0
    width = max(len(name) for name, *_ in items) + 4

    while True:
        # Render
        sys.stdout.write("\033[?25l")          # esconde cursor
        sys.stdout.write("\033[2J\033[H")      # limpa tela, cursor home

        # Cabeçalho
        title = "Windows Setup" if IS_WINDOWS else "Linux Setup"
        subtitle = "↑↓ navega  Enter seleciona  ESC/Sair sai"
        inner_w = max(len(title), len(subtitle))
        bar = "═" * (inner_w + 4)

        print(f"  ╔{bar}╗")
        print(f"  ║  {title:<{inner_w}}  ║")
        print(f"  ║  {subtitle:<{inner_w}}  ║")
        print(f"  ╚{bar}╝")
        print()

        for i, (name, desc, _needs_admin) in enumerate(items):
            prefix = "  ▸ " if i == current else "    "
            line = f"{prefix}{name:<{width}} {desc}"
            if i == current:
                print(f"\033[7m{line}\033[0m")  # invertido = destaque
            else:
                print(line)

        print(f"\n  {current + 1}/{len(items)}")

        # Le tecla
        key = _get_key()

        if key == "\x1b":                       # ESC → sai
            sys.stdout.write("\033[?25h")       # mostra cursor
            print("\nSaindo do menu.")
            _SKIP_PAUSE = True
            break

        elif key == "\x1b[A" or key == "\xe0H":  # ↑
            current = (current - 1) % len(items)

        elif key == "\x1b[B" or key == "\xe0P":  # ↓
            current = (current + 1) % len(items)

        elif key in ("\r", "\n"):                # Enter
            name, desc, needs_admin = items[current]
            if name == "sair":
                sys.stdout.write("\033[?25h")    # mostra cursor
                print("\nSaindo do menu.")
                _SKIP_PAUSE = True
                break
            if needs_admin and IS_WINDOWS and not is_admin():
                print(f"\n'{name}' requer privilégios de administrador.")
                print(f"Execute no terminal elevado: "
                      f"python {SCRIPT_PATH} {name}")
                pause()
                continue

            sys.stdout.write("\033[?25h")        # mostra cursor
            print(f"\nExecutando: {desc}...")
            func = TASKS.get(name)
            if func:
                try:
                    func()
                except SystemExit:
                    pass  # ensure_privileges() fez sys.exit após UAC
                except Exception as e:
                    print(f"\nErro: {e}")
                pause()  # pausa para usuário ler saída antes do menu redesenhar
            else:
                print(f"Tarefa '{name}' não encontrada.")
                pause()


# ---- Privilégios / elevação ----

def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.geteuid() == 0


def relaunch_as_admin_windows():
    params = " ".join(sys.argv[1:])

    # Cria .bat temporário — mais confiável que cmd /c com aspas
    bat_path = Path(tempfile.mktemp(suffix=".bat"))
    bat_path.write_text(
        f'@echo off\r\n'
        f'"{sys.executable}" "{SCRIPT_PATH}" {params}\r\n'
        f'pause\r\n'
        f'del "%~f0" & exit\r\n'
    )

    print("Privilégios de Administrador necessários. Solicitando elevação (UAC)...")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", str(bat_path), "", None, 1,
    )
    if ret <= 32:
        bat_path.unlink(missing_ok=True)
        print("Elevação cancelada ou falhou. Encerrando.")
        sys.exit(1)
    sys.exit(0)


def ensure_privileges():
    """Eleva privilégios no Windows (UAC), a menos que --no-elevate."""
    if IS_WINDOWS:
        if "--no-elevate" in sys.argv:
            return
        if not is_admin():
            relaunch_as_admin_windows()
        else:
            print("Executando com privilégios de Administrador. OK.")
    else:
        if is_admin():
            print("AVISO: rodando como root diretamente. O script já usa 'sudo' "
                  "pontualmente onde necessário.")


# ==========================================================================
# Detecção de ambiente
# ==========================================================================

_distro_cache: dict[str, str] | None = None


def _load_distro_info() -> dict[str, str]:
    """Carrega e cacheia informações da distro a partir de /etc/os-release."""
    global _distro_cache
    if _distro_cache is not None:
        return _distro_cache

    _distro_cache = {}
    if IS_WINDOWS:
        return _distro_cache

    os_release = Path("/etc/os-release")
    if not os_release.is_file():
        return _distro_cache

    with open(os_release) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            _distro_cache[key] = value

    return _distro_cache


def distro_id() -> str:
    """Retorna o ID da distribuição (ex: 'debian', 'almalinux', 'ubuntu')."""
    return _load_distro_info().get("ID", "")


def distro_codename() -> str:
    """Retorna o VERSION_CODENAME (ex: 'bookworm', 'trixie')."""
    return _load_distro_info().get("VERSION_CODENAME", "")


def distro_version_id() -> str:
    """Retorna o VERSION_ID (ex: '12', '9')."""
    return _load_distro_info().get("VERSION_ID", "")


# ---- Detecção de distribuição ----

def is_debian() -> bool:
    return distro_id() == "debian"


def is_debian_12() -> bool:
    return is_debian() and distro_codename() == "bookworm"


def is_debian_13() -> bool:
    return is_debian() and distro_codename() == "trixie"


def is_debian_14() -> bool:
    return is_debian() and distro_codename() == "forky"


def is_debian_15() -> bool:
    return is_debian() and distro_codename() == "duke"


def is_almalinux() -> bool:
    return distro_id() == "almalinux"


def is_almalinux_9() -> bool:
    return is_almalinux() and distro_version_id().startswith("9")


def is_almalinux_10() -> bool:
    return is_almalinux() and distro_version_id().startswith("10")


def has_debian_repo(component: str) -> bool:
    """Verifica se um componente (non-free, contrib, etc.) está no sources.list."""
    if not is_debian():
        return False
    sources_list = Path("/etc/apt/sources.list")
    if not sources_list.is_file():
        return False
    try:
        text = sources_list.read_text()
    except OSError:
        return False
    # Procura linhas: deb ... main ... <component>
    pattern = re.compile(
        rf"^deb\s+.*\bmain\b.*\b{re.escape(component)}\b", re.MULTILINE
    )
    return bool(pattern.search(text))


# ---- Detecção de hardware / sessão ----

def is_laptop() -> bool:
    """Detecta se está rodando em um laptop/notebook."""
    if IS_WINDOWS:
        try:
            result = run(
                [
                    "powershell", "-Command",
                    "(Get-CimInstance Win32_SystemEnclosure).ChassisTypes",
                ],
                capture=True,
            )
            # ChassisTypes: 8=Portable, 9=Laptop, 10=Notebook, 14=SubNotebook
            types_str = result.stdout.strip()
            if types_str:
                chassis = {int(x) for x in types_str.split() if x.isdigit()}
                return bool(chassis & {8, 9, 10, 14})
        except Exception:
            pass
        return False

    try:
        result = run(["hostnamectl", "chassis"], capture=True)
        return result.stdout.strip() == "laptop"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "")


def is_x11() -> bool:
    return _session_type() == "x11"


def is_wayland() -> bool:
    return _session_type() == "wayland"


def is_gnome() -> bool:
    return os.environ.get("XDG_CURRENT_DESKTOP", "") == "GNOME"


def is_windows() -> bool:
    return IS_WINDOWS


# ---- Comando de instalação ----

def get_install_cmd() -> list[str]:
    """Retorna o comando de instalação de pacotes adequado à plataforma.

    Windows → winget
    Debian/Ubuntu → apt
    AlmaLinux/Rocky/RHEL → dnf
    """
    if IS_WINDOWS:
        return ["winget", "install"]

    did = distro_id()
    if did in ("debian", "ubuntu"):
        return ["sudo", "apt", "install", "-y"]
    elif did in ("almalinux", "rocky", "rhel", "fedora", "centos"):
        return ["sudo", "dnf", "install", "-y"]
    else:
        # Fallback: tenta apt (mais comum em derivados Debian)
        print(f"AVISO: distro '{did}' desconhecida, usando 'apt' como fallback.")
        return ["sudo", "apt", "install"]


# ==========================================================================
# Setup: Syncthing
# ==========================================================================

def get_syncthing_root() -> Path:
    return Path(r"C:\Opt\Syncthing") if IS_WINDOWS else Path("/opt/Syncthing")


def get_syncthing_link_path() -> Path:
    return Path.home() / "Syncthing"


def ensure_syncthing_dir(syncthing_root: Path):
    if not IS_WINDOWS:
        run(["sudo", "mkdir", "-p", str(syncthing_root)])
        user = current_user()
        if user:
            run(["sudo", "chown", f"{user}:{user}", str(syncthing_root)])
    else:
        syncthing_root.mkdir(parents=True, exist_ok=True)


def create_symlink(link_path: Path, target: Path):
    if link_path.exists() or link_path.is_symlink():
        print(f"Link {link_path} já existe, pulando criação.")
        return
    try:
        link_path.symlink_to(target, target_is_directory=True) if IS_WINDOWS \
            else link_path.symlink_to(target)
        print(f"Link criado: {link_path} -> {target}")
    except OSError as e:
        print(f"AVISO: não foi possível criar o link simbólico ({e}).")
        if IS_WINDOWS:
            print("Verifique se está rodando como Administrador ou com o "
                  "Modo Desenvolvedor ativado.")


def find_syncthing() -> str | None:
    """Localiza o binário do syncthing no PATH ou em locais conhecidos."""
    path = shutil.which("syncthing")
    if path:
        return path
    if not IS_WINDOWS:
        return None

    # Busca em diretórios comuns (USERPROFILE é confiável mesmo elevado)
    candidates = [
        r"C:\Program Files\Syncthing\syncthing.exe",
        r"C:\Program Files (x86)\Syncthing\syncthing.exe",
    ]
    userprofile = os.environ.get("USERPROFILE", "")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    for base in {userprofile, local_app_data}:
        if not base:
            continue
        candidates.append(
            str(Path(base) / "Programs" / "Syncthing" / "syncthing.exe")
        )
        try:
            for match in Path(base).glob("**/syncthing.exe"):
                candidates.append(str(match))
        except OSError:
            pass
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def ensure_syncthing_in_path():
    """Garante que o diretório do syncthing está no PATH do processo."""
    if shutil.which("syncthing"):
        return
    exe = find_syncthing()
    if exe:
        parent = str(Path(exe).parent)
        os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")
        print(f"  → {exe} adicionado ao PATH")


def enable_syncthing_service():
    if IS_WINDOWS:
        syncthing_exe = find_syncthing()
        if not syncthing_exe:
            print("AVISO: syncthing.exe não encontrado. Pulando serviço.")
            return

        # Adiciona à inicialização do Windows (HKCU Run — não precisa de admin)
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
            )
            try:
                winreg.DeleteValue(key, "Syncthing")
            except FileNotFoundError:
                pass
            winreg.SetValueEx(key, "Syncthing", 0, winreg.REG_SZ,
                              f'"{syncthing_exe}" serve --no-browser --no-console')
            winreg.CloseKey(key)
            print("Syncthing adicionado à inicialização do Windows (registro).")
        except OSError as e:
            print(f"AVISO: não foi possível adicionar à inicialização: {e}")

        # Inicia syncthing em segundo plano (serve é o comando daemon)
        print(f"Iniciando syncthing: {syncthing_exe}")
        proc = subprocess.Popen(
            [syncthing_exe, "serve", "--no-browser"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Aguarda e verifica se o processo está rodando
        time.sleep(3)
        if proc.poll() is not None:
            print(f"ERRO: syncthing encerrou com código {proc.returncode}")
            print("Verifique se outra instância já está rodando ou se a "
                  "porta 8384 está em uso.")
        elif is_syncthing_running():
            print("Syncthing iniciado com sucesso.")
        else:
            print("AVISO: syncthing está rodando mas não respondeu ao cli "
                  f"(PID {proc.pid}). Aguardando...")
    else:
        user = current_user()
        run(sudo_prefix() + ["systemctl", "enable", "--now", f"syncthing@{user}"], check=True)


def start_syncthing_daemon():
    """Inicia o processo do syncthing (sem mexer no registro/systemctl enable).

    Diferente de enable_syncthing_service(), esta função só sobe o daemon,
    sem registrar para inicialização automática. Usada no setup para
    garantir que o daemon está rodando sem reaplicar configuração de serviço.
    """
    if IS_WINDOWS:
        syncthing_exe = find_syncthing()
        if not syncthing_exe:
            print("AVISO: syncthing.exe não encontrado.")
            return
        print(f"Iniciando syncthing: {syncthing_exe}")
        proc = subprocess.Popen(
            [syncthing_exe, "serve", "--no-browser"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        if proc.poll() is not None:
            print(f"ERRO: syncthing encerrou com código {proc.returncode}")
            print("Verifique se outra instância já está rodando ou se a "
                  "porta 8384 está em uso.")
        elif is_syncthing_running():
            print("Syncthing iniciado com sucesso.")
        else:
            print("AVISO: syncthing está rodando mas não respondeu ao cli "
                  f"(PID {proc.pid}). Aguardando...")
    else:
        user = current_user()
        run(sudo_prefix() + ["systemctl", "start", f"syncthing@{user}"], check=True)


def get_local_device_id() -> str | None:
    """Retorna o device ID local (consulta o daemon, não precisa do serviço rodando).

    Tenta --device-id (Linux/versões recentes) e device-id (Windows/fallback).
    """
    ensure_syncthing_in_path()
    for flag in ("--device-id", "device-id"):
        try:
            result = run(["syncthing", flag], capture=True)
            output = (result.stdout.strip() or result.stderr.strip() or "")
            if result.returncode == 0:
                for line in output.splitlines():
                    line = line.strip()
                    if line and not line.startswith("Error"):
                        return line
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return None


def is_syncthing_running() -> bool:
    """Verifica se o daemon do syncthing está respondendo."""
    ensure_syncthing_in_path()
    try:
        result = run(["syncthing", "cli", "config", "devices", "list"],
                     capture=True)
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def syncthing_install():
    """Instala o syncthing e prepara diretórios/serviço (sem configurar pastas)."""
    print("\n=== Instalação do Syncthing ===\n")
    ensure_privileges()

    if not find_syncthing():
        print("syncthing não encontrado. Instalando...")
        if IS_WINDOWS:
            run(["winget", "install", "--accept-source-agreements",
                 "--accept-package-agreements", "Syncthing.Syncthing"])
        elif is_debian() or distro_id() in ("ubuntu",):
            # Adiciona repositório oficial para obter a versão mais recente
            sources_list = "/etc/apt/sources.list.d/syncthing.list"
            keyring = "/etc/apt/keyrings/syncthing-archive-keyring.gpg"
            if not os.path.isfile(sources_list):
                print("Adicionando repositório oficial do Syncthing...")
                run(sudo_prefix() + ["mkdir", "-p", "/etc/apt/keyrings"])
                import urllib.request
                tmp_key = "/tmp/syncthing-keyring.gpg"
                urllib.request.urlretrieve(
                    "https://syncthing.net/release-key.gpg", tmp_key)
                run(sudo_prefix() + ["cp", tmp_key, keyring])
                os.unlink(tmp_key)
                sudo_write(
                    sources_list,
                    "deb [signed-by=/etc/apt/keyrings/syncthing-archive-keyring.gpg] "
                    "https://apt.syncthing.net/ syncthing stable-v2\n"
                )
                run(sudo_prefix() + ["apt", "update"])
            run(["sudo", "apt", "install", "-y", "syncthing"])
        elif is_almalinux():
            run(sudo_prefix() + ["dnf", "copr", "enable", "-y",
                                 "syncthing/syncthing"])
            run(sudo_prefix() + ["dnf", "install", "-y", "syncthing"])
        else:
            print("Distro não reconhecida. Instale o syncthing manualmente: "
                  "https://syncthing.net/downloads/")
            return
        ensure_syncthing_in_path()
        if not find_syncthing():
            print("Falha ao instalar o syncthing. Verifique e tente novamente.")
            return

    syncthing_root = get_syncthing_root()
    link_path = get_syncthing_link_path()

    ensure_syncthing_dir(syncthing_root)
    create_symlink(link_path, syncthing_root)

    run(["syncthing", "generate"])
    enable_syncthing_service()

    # Aguarda o syncthing responder (até 30s no primeiro start)
    print("Aguardando o syncthing iniciar...")
    running = False
    for _ in range(30):
        time.sleep(1)
        if is_syncthing_running():
            running = True
            break
        print(".", end="", flush=True)

    local_id = get_local_device_id()
    if not running or not local_id:
        print("\nNão foi possível conectar ao Syncthing. "
              "Verifique se o serviço está rodando.")
        return

    print(f"\nSyncthing instalado e rodando. LOCAL_ID: {local_id}")
    print("Use 'syncthing-setup' para configurar as pastas de sincronização.")


def syncthing_setup():
    """Configura o syncthing: adiciona dispositivo servidor e pasta BUILDS."""
    print("\n=== Configuração do Syncthing ===\n")

    # Garante que o binário está localizável (pode ter sido instalado
    # em outra sessão e não estar mais no PATH)
    ensure_syncthing_in_path()
    if not shutil.which("syncthing"):
        print("Syncthing não encontrado. Execute 'syncthing-install' primeiro:")
        print(f"  python {SCRIPT_PATH} syncthing-install")
        pause()
        return

    # Device ID (offline — não precisa do daemon)
    local_id = get_local_device_id()
    if not local_id:
        print("Não foi possível obter o device ID. O syncthing está instalado?")
        pause()
        return

    # Daemon precisa estar rodando para os comandos cli config.
    # Sobe o processo se necessário (sem reaplicar registro/systemctl enable,
    # que já foram feitos pelo syncthing-install).
    if not is_syncthing_running():
        print("O daemon do syncthing não está respondendo. Iniciando...")
        start_syncthing_daemon()
        for _ in range(15):
            time.sleep(1)
            if is_syncthing_running():
                break
            print(".", end="", flush=True)
        else:
            print("\nNão foi possível conectar ao daemon do syncthing.")
            print("Reinicie o computador ou execute 'syncthing-install'.")
            pause()
            return

    server_id = prompt_with_default(
        "Digite o ID do servidor Syncthing (SERVER_ID)", DEFAULT_SERVER_ID
    )
    folder_id = prompt_with_default(
        "Digite o ID da pasta do servidor Syncthing (FOLDER_ID)", DEFAULT_FOLDER_ID
    )

    print("\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("Execute no servidor Syncthing:\n")
    print(f"  syncthing cli config devices add --device-id {local_id}\n")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    pause()

    run(["syncthing", "cli", "config", "devices", "add", "--device-id", server_id])
    run(["syncthing", "cli", "config", "devices", server_id,
         "auto-accept-folders", "set", "true"])

    syncthing_root = get_syncthing_root()
    builds_path = syncthing_root / "BUILDS"
    builds_path.mkdir(parents=True, exist_ok=True)

    run([
        "syncthing", "cli", "config", "folders", "add",
        "--id", folder_id, "--label", "BUILDS",
        "--path", str(builds_path), "--type", "receiveonly",
    ])

    print("\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("Execute no servidor Syncthing:\n")
    print(f"  syncthing cli config folders {folder_id} devices add --device-id {local_id}\n")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
    pause()

    run(["syncthing", "cli", "config", "devices", server_id,
         "auto-accept-folders", "set", "false"])
    enable_syncthing_service()

    print("Configuração do Syncthing concluída com sucesso.")


# ==========================================================================
# Setup: Tailscale
# ==========================================================================

def find_tailscale() -> str | None:
    path = shutil.which("tailscale")
    if path:
        return path
    if IS_WINDOWS:
        candidates = [
            r"C:\Program Files\Tailscale\tailscale.exe",
            r"C:\Program Files (x86)\Tailscale\tailscale.exe",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


def enable_tailscaled_service():
    if IS_WINDOWS:
        result = run(["sc", "query", "Tailscale"], capture=True)
        if result.returncode == 0:
            run(["net", "start", "Tailscale"])
        else:
            print("AVISO: serviço 'Tailscale' não encontrado no Windows.")
    else:
        run(sudo_prefix() + ["systemctl", "enable", "--now", "tailscaled"], check=True)


def tailscale_up(tailscale_bin: str, tailkey: str) -> bool:
    cmd = sudo_prefix() + [tailscale_bin, "up", f"--auth-key={tailkey}"]
    if run(cmd).returncode == 0:
        return True

    print("Falha com --auth-key, tentando 'tailscale up' interativo...")
    cmd = sudo_prefix() + [tailscale_bin, "up"]
    return run(cmd).returncode == 0


def tailscale_set(tailscale_bin: str):
    cmd = sudo_prefix() + [
        tailscale_bin, "set", "--accept-dns", "--auto-update", "--update-check",
    ]
    if not IS_WINDOWS:
        user = current_user()
        if user:
            cmd.append(f"--operator={user}")
    run(cmd, check=True)


def setup_tailscale():
    print("\n=== Setup Tailscale ===\n")
    ensure_privileges()

    if DEFAULT_TAILKEY:
        tailkey = prompt_with_default("Digite a chave para workstations", DEFAULT_TAILKEY)
    else:
        tailkey = input("Digite a chave para workstations: ").strip()
    if not tailkey:
        print("Chave não informada. Pulando configuração do Tailscale.")
        return

    tailscale_bin = find_tailscale()
    if not tailscale_bin:
        print("tailscale não encontrado. Instalando...")
        if IS_WINDOWS:
            run(["winget", "install", "--accept-source-agreements",
                 "--accept-package-agreements", "Tailscale.Tailscale"])
        elif is_debian() or distro_id() in ("ubuntu",):
            curl = subprocess.run(
                ["curl", "-fsSL", "https://tailscale.com/install.sh"],
                capture_output=True, text=True, check=True,
            )
            subprocess.run(["sh"], input=curl.stdout, text=True)
        elif is_almalinux():
            ver = distro_version_id()
            run(sudo_prefix() + [
                "dnf", "config-manager", "--add-repo",
                f"https://pkgs.tailscale.com/stable/rhel/{ver}/tailscale.repo",
            ])
            run(sudo_prefix() + ["dnf", "install", "-y", "tailscale"])
        else:
            print("Distro não reconhecida. Instale o tailscale manualmente: "
                  "https://tailscale.com/download/")
            return
        _refresh_path_if_needed("tailscale")
        tailscale_bin = find_tailscale()
        if not tailscale_bin:
            print("Falha ao instalar o tailscale. Verifique e tente novamente.")
            return

    enable_tailscaled_service()

    # Aguarda o daemon do Tailscale responder (pode demorar no primeiro start)
    print("Aguardando o Tailscale iniciar...")
    for _ in range(15):
        time.sleep(1)
        result = run([tailscale_bin, "status"], capture=True)
        if result.returncode == 0:
            break
        print(".", end="", flush=True)
    else:
        print("\nAVISO: Tailscale não respondeu. Tentando 'tailscale up' assim mesmo...")

    ok = tailscale_up(tailscale_bin, tailkey)

    if ok:
        try:
            tailscale_set(tailscale_bin)
        except subprocess.CalledProcessError as e:
            print(f"Erro ao configurar tailscale set: {e}")

    run([tailscale_bin, "status"])
    pause()


# ==========================================================================
# Setup: Pacotes
# ==========================================================================

# Constantes de impressoras (espelham o fishrc)
FISH_PRINTSERVER = "fishnote00"
FISH_PRINTERS = ["M1180_01", "M1180_02", "M1180_03", "M3180_04"]


def get_main_packages() -> list[str]:
    """Retorna a lista de pacotes sugeridos para a plataforma/distro atual."""
    pkgs = ["curl", "espeak-ng", "libreoffice-java-common",
            "python3-virtualenv", "rclone", "restic", "rsync", "strace",
            "wget", "wl-clipboard"]

    if IS_WINDOWS:
        return ["Rclone.Rclone", "Restic.Restic", "GNU.Wget2"]

    pkgs.append("system-config-printer")
    if is_x11():
        pkgs.append("xclip")

    if is_debian():
        pkgs.append("libcupsimage2t64")
        if is_debian_13():
            pkgs.extend(["python3-dev", "python3-venv"])
        if is_laptop():
            pkgs.append("lm-sensors")
        pkgs.extend(["font-manager", "fonts-anonymous-pro", "unifont"])
        pkgs.append("sqlitebrowser")

    if has_debian_repo("non-free"):
        pkgs.append("fonts-ubuntu-console")
    if has_debian_repo("contrib"):
        pkgs.append("ttf-mscorefonts-installer")

    if is_almalinux():
        pkgs.append("libunwind")
        pkgs.extend(["font-manager", "msimonson-anonymouspro-fonts", "gtk3"])
        if is_almalinux_10():
            pkgs.append("webkit2gtk4.1")
        if is_laptop():
            pkgs.append("lm_sensors")

    return pkgs


def setup_packages():
    """Instala os pacotes sugeridos para a plataforma/distro atual."""
    print("\n=== Instalação de pacotes ===\n")
    if IS_WINDOWS:
        ensure_privileges()

    # Debian: adiciona contrib e non-free se necessário
    if is_debian():
        _ensure_debian_contrib_nonfree()

    pkgs = get_main_packages()
    if not pkgs:
        print("Nenhum pacote sugerido para esta plataforma.")
        pause()
        return

    install_cmd = get_install_cmd()

    print(f"Pacotes a instalar ({len(pkgs)}):")
    for p in pkgs:
        print(f"  - {p}")
    resposta = input("Continuar? [s/N]: ").strip().lower()
    if resposta not in ("s", "sim", "y", "yes"):
        print("Cancelado.")
        return

    # Instala um por um para que falhas em pacotes específicos
    # (ex: não disponíveis na distro) não impeçam os demais
    for pkg in pkgs:
        run(install_cmd + [pkg])

    print("Instalação de pacotes concluída.")


def _ensure_debian_contrib_nonfree():
    """Adiciona contrib e non-free aos repositórios Debian se ausentes."""
    sources_path = Path("/etc/apt/sources.list")
    if not sources_path.is_file():
        return

    text = sources_path.read_text()
    updated = []
    changed = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("deb "):
            # Verifica tokens exatos (evita falso positivo em non-free-firmware)
            tokens = stripped.split()
            has_contrib = "contrib" in tokens
            has_nonfree = "non-free" in tokens
            if not has_contrib and not has_nonfree:
                line = line.rstrip() + " contrib non-free"
                changed = True
            elif not has_contrib:
                line = line.rstrip() + " contrib"
                changed = True
            elif not has_nonfree:
                line = line.rstrip() + " non-free"
                changed = True
        updated.append(line)

    if changed:
        print("Adicionando contrib e non-free aos repositórios...")
        sudo_write(sources_path, "\n".join(updated) + "\n")
        run(sudo_prefix() + ["apt", "update"])


# ==========================================================================
# Setup: Laptop
# ==========================================================================

def setup_laptop():
    """Configurações específicas para laptops (systemd, suspensão, lm-sensors)."""
    print("\n=== Configurações para laptop ===\n")

    if IS_WINDOWS:
        print("Configurações de laptop no Windows ainda não implementadas.")
        pause()
        return

    if not is_laptop():
        print("Este computador não parece ser um laptop. Pulando.")
        return

    install_cmd = get_install_cmd()

    # lm-sensors
    if is_debian():
        run(install_cmd + ["lm-sensors"])
        run(install_cmd + ["gnome-shell-extension-freon"])
    elif is_almalinux():
        run(install_cmd + ["lm_sensors"])

    # Lid switch — não desligar ao fechar a tampa
    for key in ["HandleLidSwitch", "HandleLidSwitchExternalPower"]:
        run(sudo_prefix() + [
            "sed", "-i",
            f"s/^#*{key}=.*/{key}=ignore/",
            "/etc/systemd/logind.conf",
        ])

    # Desabilitar suspensão/hibernação
    run(sudo_prefix() + ["mkdir", "-p", "/etc/systemd/sleep.conf.d/"])
    sudo_write(
        "/etc/systemd/sleep.conf.d/nosuspend.conf",
        "[Sleep]\n"
        "AllowSuspend=no\n"
        "AllowHibernation=no\n"
        "AllowSuspendThenHibernate=no\n"
        "AllowHybridSleep=no\n",
    )

    print("Configurações de laptop concluídas.")


# ==========================================================================
# Setup: GNOME / GTK
# ==========================================================================

def setup_gtk():
    """Customiza o GNOME/GTK para melhorar a experiência do Fish4."""
    print("\n=== Customização GNOME/GTK ===\n")

    if IS_WINDOWS:
        print("GNOME não está disponível no Windows. Pulando.")
        pause()
        return

    if not is_gnome():
        print("GNOME não detectado. Pulando.")
        return

    # Notificações e comportamento
    run(["gsettings", "set", "org.gnome.desktop.notifications",
         "show-in-lock-screen", "false"])
    run(["gsettings", "set", "org.gnome.mutter",
         "check-alive-timeout", "120000"])
    run(["gsettings", "set", "org.gnome.shell",
         "always-show-log-out", "true"])

    # Imagens em botões e menus (GTK)
    if is_debian_12() or is_almalinux_9():
        run(["gsettings", "set",
             "org.gnome.settings-daemon.plugins.xsettings",
             "overrides",
             "{'Gtk/ButtonImages': <1>, 'Gtk/MenuImages': <1>}"])
    elif is_debian_13() or is_debian_14() or is_almalinux_10():
        gtk3_dir = Path.home() / ".config/gtk-3.0"
        gtk3_dir.mkdir(parents=True, exist_ok=True)
        ini = gtk3_dir / "settings.ini"
        ini.write_text(
            "[Settings]\ngtk-menu-images = true\ngtk-button-images = true\n"
        )
        print(f"  → {ini}")

    print("Customização GNOME/GTK concluída.")


# ==========================================================================
# Setup: Extensões GNOME
# ==========================================================================

# Extensões instaladas via gerenciador de pacotes (apt/dnf)
_GNOME_EXT_PACKAGES = [
    "gnome-shell-extension-appindicator",
    "gnome-shell-extension-dashtodock",
    "gnome-shell-extension-caffeine",
]
# UUIDs correspondentes às extensões empacotadas (para habilitar)
_GNOME_EXT_PACKAGE_UUIDS = [
    "ubuntu-appindicators@ubuntu.com",
    "dash-to-dock@micxgx.gmail.com",
    "caffeine@patapon.info",
]
# Extensões baixadas diretamente do extensions.gnome.org
_GNOME_EXT_DOWNLOAD_UUIDS = [
    "add-username-toppanel@brendaw.com",
    "BingWallpaper@ineffable-gmail.com",
    "lockkeys@vaina.lt",
    "systemd-status@ne0sight.github.io",
    "tailscale@joaophi.github.com",
]


def _get_gnome_shell_version() -> str:
    """Retorna a versão major do GNOME Shell (ex: '44')."""
    try:
        result = subprocess.run(
            ["gnome-shell", "--version"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    match = re.search(r"(\d+)", result.stdout)
    return match.group(1) if match else ""


def setup_gnome_extensions():
    """Instala as extensões GNOME sugeridas (pacotes + extensions.gnome.org)."""
    print("\n=== Instalação de extensões GNOME ===\n")

    if IS_WINDOWS:
        print("GNOME não está disponível no Windows. Pulando.")
        pause()
        return

    if not is_gnome():
        print("GNOME não detectado. Pulando.")
        return

    install_cmd = get_install_cmd()

    # --- Extensões empacotadas ---
    packages = list(_GNOME_EXT_PACKAGES)
    if is_laptop():
        packages.append("gnome-shell-extension-freon")

    print("Instalando extensões empacotadas...")
    for pkg in packages:
        run(install_cmd + [pkg])

    # --- Extensões do extensions.gnome.org ---
    gnome_ver = _get_gnome_shell_version()
    if not gnome_ver:
        print("AVISO: não foi possível detectar a versão do GNOME Shell.")
        print("Pulando extensões do extensions.gnome.org.")
        pause()
        return

    print(f"\nGNOME Shell versão {gnome_ver} detectado.")
    print("Baixando extensões do extensions.gnome.org...")

    import urllib.request
    import urllib.error

    # Lista de extensões já instaladas
    result = subprocess.run(
        ["gnome-extensions", "list", "--user"],
        capture_output=True, text=True,
    )
    installed = set(result.stdout.strip().splitlines())

    for ext_uuid in _GNOME_EXT_DOWNLOAD_UUIDS:
        if ext_uuid in installed:
            print(f"  {ext_uuid} — já instalada, pulando.")
            continue

        print(f"  {ext_uuid} — baixando...")
        try:
            # Consulta a API do extensions.gnome.org
            query_url = (
                "https://extensions.gnome.org/extension-query/"
                f"?search={ext_uuid}"
            )
            req = urllib.request.Request(query_url)
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())

            # Encontra o version_tag para esta versão do GNOME Shell
            version_tag = None
            for ext in data.get("extensions", []):
                if ext.get("uuid") == ext_uuid:
                    shell_map = ext.get("shell_version_map", {})
                    shell_entry = shell_map.get(gnome_ver, {})
                    version_tag = shell_entry.get("pk")
                    break

            if not version_tag:
                print(f"    → versão para GNOME {gnome_ver} não encontrada, "
                      f"pulando.")
                continue

            # Download do zip
            download_url = (
                "https://extensions.gnome.org/download-extension/"
                f"{ext_uuid}.shell-extension.zip?version_tag={version_tag}"
            )
            zip_path = Path(tempfile.mktemp(suffix=".zip"))
            urllib.request.urlretrieve(download_url, zip_path)

            # Instala
            subprocess.run(
                ["gnome-extensions", "install", "--force", str(zip_path)],
                check=True,
            )
            zip_path.unlink()
            print(f"    → instalada com sucesso.")

        except (urllib.error.URLError, subprocess.CalledProcessError,
                json.JSONDecodeError, OSError) as e:
            print(f"    → erro: {e}")

    # Reiniciar sessão GNOME para aplicar
    pause()
    print("\nSua sessão GNOME será reiniciada para aplicar as extensões...")
    resposta = input("Reiniciar agora? [s/N]: ").strip().lower()
    if resposta in ("s", "sim", "y", "yes"):
        user = current_user() or os.environ.get("USER", "")
        if user:
            subprocess.run(["loginctl", "terminate-user", user])
    else:
        print("Reinicie a sessão GNOME manualmente quando conveniente.")


def setup_gnome_enable_extensions():
    """Habilita todas as extensões GNOME sugeridas que estejam instaladas."""
    print("\n=== Habilitar extensões GNOME ===\n")

    if IS_WINDOWS:
        print("GNOME não está disponível no Windows. Pulando.")
        pause()
        return

    if not is_gnome():
        print("GNOME não detectado. Pulando.")
        return

    all_uuids = list(_GNOME_EXT_PACKAGE_UUIDS)
    if is_laptop():
        all_uuids.append("freon@UshakovVasilii_Github.yahoo.com")
    all_uuids.extend(_GNOME_EXT_DOWNLOAD_UUIDS)

    result = subprocess.run(
        ["gnome-extensions", "list", "--enabled"],
        capture_output=True, text=True,
    )
    enabled = set(result.stdout.strip().splitlines())

    result = subprocess.run(
        ["gnome-extensions", "list", "--user"],
        capture_output=True, text=True,
    )
    user_installed = set(result.stdout.strip().splitlines())

    for uuid in all_uuids:
        if uuid in enabled:
            print(f"  {uuid} — já habilitada, pulando.")
            continue
        if uuid not in user_installed:
            print(f"  {uuid} — não está instalada, pulando.")
            continue
        subprocess.run(["gnome-extensions", "enable", uuid], check=True)
        print(f"  {uuid} — habilitada.")

    pause()


# ==========================================================================
# Setup: Keyd (mapear Enter e , no teclado numérico)
# ==========================================================================

def setup_keyd():
    """Instala e configura o keyd para teclado numérico."""
    print("\n=== Setup Keyd (teclado numérico) ===\n")

    if IS_WINDOWS:
        print("Keyd não está disponível no Windows. Pulando.")
        pause()
        return

    install_cmd = get_install_cmd()

    if is_debian_13():
        run(install_cmd + ["keyd"])
    elif is_debian_12():
        # Remapeamento X11 (sem pacote keyd no Debian 12)
        remap = "keycode 104 = Return"
        xmodmap_file = Path.home() / ".Xmodmap"
        existing = xmodmap_file.read_text() if xmodmap_file.is_file() else ""
        if remap not in existing:
            with open(xmodmap_file, "a") as f:
                f.write(remap + "\n")
            print(f"  → {xmodmap_file}")
        bashrc_line = (
            '[[ "$XDG_SESSION_TYPE" = "x11" ]] && '
            '[ -f ~/.Xmodmap ] && xmodmap ~/.Xmodmap'
        )
        bashrc = Path.home() / ".bashrc"
        bashrc_text = bashrc.read_text() if bashrc.is_file() else ""
        if bashrc_line not in bashrc_text:
            with open(bashrc, "a") as f:
                f.write(bashrc_line + "\n")
            print(f"  → {bashrc}")
    elif is_almalinux():
        run(install_cmd + ["git", "gcc", "make"])
        keyd_dir = Path.home() / "keyd"
        if keyd_dir.is_dir():
            run(["git", "-C", str(keyd_dir), "pull"])
        else:
            run(["git", "clone", "https://github.com/rvaiya/keyd",
                 str(keyd_dir)])
        os.chdir(keyd_dir)
        run(["make"])
        run(sudo_prefix() + ["make", "install"])
        os.chdir(Path.home())

    # Configuração do keyd
    if is_debian_13() or is_almalinux():
        run(sudo_prefix() + ["mkdir", "-p", "/etc/keyd"])
        keyd_conf = Path("/etc/keyd/default.conf")
        if not keyd_conf.is_file():
            sudo_write(
                "/etc/keyd/default.conf",
                "[ids]\n*\n\n[main]\nkpenter = enter\nkpdot = ,\n",
            )
        run(sudo_prefix() + ["systemctl", "enable", "--now", "keyd"])

    print("Setup keyd concluído.")


# ==========================================================================
# Setup: CUPS
# ==========================================================================

def setup_cups():
    """Configura o CUPS (retenção de jobs, JobPrivateValues)."""
    print("\n=== Configuração CUPS ===\n")

    if IS_WINDOWS:
        print("CUPS não está disponível no Windows. Pulando.")
        pause()
        return

    cupsd_conf = "/etc/cups/cupsd.conf"

    # JobPrivateValues none
    run(sudo_prefix() + [
        "bash", "-c",
        f"grep -qi 'JobPrivateValues none' {cupsd_conf} || "
        f"sed -i 's/JobPrivateValues .*/JobPrivateValues none/' {cupsd_conf}",
    ])

    # PreserveJobHistory
    run(sudo_prefix() + [
        "bash", "-c",
        f"grep -qi PreserveJobHistory {cupsd_conf} || "
        f"sed -i '1i PreserveJobHistory 129600' {cupsd_conf}",
    ])

    print("Configuração CUPS concluída.")


def cups_enable(enable: bool = True):
    """Habilita ou desabilita as filas de impressão CUPS."""
    if IS_WINDOWS:
        print("CUPS não está disponível no Windows.")
        return

    for printer in FISH_PRINTERS:
        if enable:
            run(["sudo", "/usr/sbin/cupsenable", printer])
        else:
            run(["sudo", "/usr/sbin/cupsdisable", printer])
    run(["lpstat", "-p"])


def setup_printers_add():
    """Adiciona as impressoras do Fish4 ao CUPS."""
    print("\n=== Adicionar impressoras ===\n")

    if IS_WINDOWS:
        print("CUPS não está disponível no Windows. Pulando.")
        pause()
        return

    # Avahi pode travar o CUPS ao buscar PPDs
    avahi_was_active = False
    result = run(["systemctl", "is-active", "--quiet", "avahi-daemon"])
    if result.returncode == 0:
        run(sudo_prefix() + ["systemctl", "stop", "avahi-daemon"])
        avahi_was_active = True

    for printer in FISH_PRINTERS:
        # Remove se já existe
        result = run(["lpstat", "-p", printer], capture=True)
        if result.returncode == 0:
            run(sudo_prefix() + ["lpadmin", "-x", printer])
        # Adiciona
        run(sudo_prefix() + [
            "lpadmin", "-p", printer, "-E",
            "-v", f"ipp://{FISH_PRINTSERVER}:631/printers/{printer}",
            "-m", "everywhere",
        ])

    if avahi_was_active:
        run(sudo_prefix() + ["systemctl", "start", "avahi-daemon"])

    print("Impressoras adicionadas.")


def setup_printers_del():
    """Remove as impressoras do Fish4 do CUPS."""
    print("\n=== Remover impressoras ===\n")

    if IS_WINDOWS:
        print("CUPS não está disponível no Windows. Pulando.")
        pause()
        return

    for printer in FISH_PRINTERS:
        run(sudo_prefix() + ["lpadmin", "-x", printer])

    print("Impressoras removidas.")


# ==========================================================================
# Setup: AWS CLI
# ==========================================================================

def setup_aws():
    """Configura a AWS CLI (access key, secret, região)."""
    print("\n=== Configuração AWS CLI ===\n")

    _refresh_path_if_needed("aws")
    if not shutil.which("aws"):
        print("Pacote awscli não está instalado.")
        print("Execute 'aws-install' primeiro ou: "
              "python setup-tools.py aws-install")
        return

    aws_key_id = input(
        "Digite a chave da conta AWS (aws_access_key_id): "
    ).strip()
    if aws_key_id:
        run(["aws", "configure", "set", "aws_access_key_id", aws_key_id])

    if aws_key_id:
        aws_secret = input(
            f"Digite o segredo da chave {aws_key_id}: "
        ).strip()
        if aws_secret:
            run(["aws", "configure", "set",
                 "aws_secret_access_key", aws_secret])

    aws_region = prompt_with_default(
        "Digite a região padrão na AWS (default.region)", "sa-east-1"
    )
    if aws_region:
        run(["aws", "configure", "set", "default.region", aws_region])

    # Teste
    print()
    if shutil.which("aws"):
        run(["aws", "sts", "get-caller-identity"])


def setup_aws_install():
    """Instala o AWS CLI (winget/apt/dnf)."""
    print("\n=== Instalação do AWS CLI ===\n")
    ensure_privileges()

    if shutil.which("aws"):
        print("AWS CLI já está instalado:")
        run(["aws", "--version"])
        return

    print("AWS CLI não encontrado. Instalando...")
    if IS_WINDOWS:
        run(["winget", "install", "--accept-source-agreements",
             "--accept-package-agreements", "Amazon.AWSCLI"])
    elif is_debian() or distro_id() in ("ubuntu",):
        run(["sudo", "apt", "install", "-y", "awscli"])
    elif is_almalinux():
        run(sudo_prefix() + ["dnf", "install", "-y", "awscli"])
    else:
        print("Distro não reconhecida. Instale o AWS CLI manualmente: "
              "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
        return

    _refresh_path_if_needed("aws")
    if shutil.which("aws"):
        print("AWS CLI instalado com sucesso:")
        run(["aws", "--version"])
    else:
        print("AVISO: AWS CLI pode ter sido instalado mas não está no PATH. "
              "Reinicie o terminal e tente novamente.")


def setup_aws_test():
    """Testa a configuração do AWS CLI executando sts get-caller-identity."""
    print("\n=== Teste AWS CLI ===\n")

    _refresh_path_if_needed("aws")
    if not shutil.which("aws"):
        print("Pacote awscli não está instalado.")
        print("Execute 'aws-install' primeiro ou: "
              "python setup-tools.py aws-install")
        return

    run(["aws", "sts", "get-caller-identity"])


# ==========================================================================
# Registry: funções de detecção expostas via --detect
# ==========================================================================

# Formato: nome → (função, número_de_argumentos)
_DETECT_FUNCTIONS: dict[str, tuple[callable, int]] = {
    # Distribuição
    "is_debian": (is_debian, 0),
    "is_debian_12": (is_debian_12, 0),
    "is_debian_13": (is_debian_13, 0),
    "is_debian_14": (is_debian_14, 0),
    "is_debian_15": (is_debian_15, 0),
    "is_almalinux": (is_almalinux, 0),
    "is_almalinux_9": (is_almalinux_9, 0),
    "is_almalinux_10": (is_almalinux_10, 0),
    "has_debian_repo": (has_debian_repo, 1),
    # Hardware / sessão
    "is_laptop": (is_laptop, 0),
    "is_windows": (is_windows, 0),
    "is_x11": (is_x11, 0),
    "is_wayland": (is_wayland, 0),
    "is_gnome": (is_gnome, 0),
}


# ==========================================================================
# Entry point
# ==========================================================================

TASKS = {
    "menu": interactive_menu,
    "syncthing-install": syncthing_install,
    "syncthing-setup": syncthing_setup,
    "tailscale": setup_tailscale,
    "packages": setup_packages,
    "laptop": setup_laptop,
    "gtk": setup_gtk,
    "extensions": setup_gnome_extensions,
    "extensions-enable": setup_gnome_enable_extensions,
    "keyd": setup_keyd,
    "cups": setup_cups,
    "cups-on": lambda: cups_enable(True),
    "cups-off": lambda: cups_enable(False),
    "printers-add": setup_printers_add,
    "printers-del": setup_printers_del,
    "aws-install": setup_aws_install,
    "aws": setup_aws,
    "aws-test": setup_aws_test,
}


def run_all():
    """Executa todas as tarefas de setup em ordem (pula menu e toggle/destrutivas)."""
    # Ordem sensata: pacotes primeiro, depois serviços, depois customizações
    order = [
        "packages",
        "laptop",
        "syncthing-install",
        "syncthing-setup",
        "tailscale",
        "aws-install",
        "aws",
        "gtk",
        "extensions",
        "extensions-enable",
        "keyd",
        "cups",
        "printers-add",
    ]
    print("\n=== Executando todas as tarefas de setup ===\n")
    print("Tarefas a executar:")
    for name in order:
        print(f"  - {name}")
    print()
    resp = input("Continuar? [s/N]: ").strip().lower()
    if resp not in ("s", "sim", "y", "yes"):
        print("Cancelado.")
        return

    for name in order:
        func = TASKS.get(name)
        if func:
            func()


def main():
    parser = argparse.ArgumentParser(
        description="Instalador/configurador multiplataforma (Syncthing / Tailscale)."
    )
    parser.add_argument(
        "task",
        nargs="?",
        choices=list(TASKS.keys()) + ["all"],
        help="Qual configuração executar.",
    )
    parser.add_argument(
        "--detect",
        choices=list(_DETECT_FUNCTIONS.keys()),
        help="Executa uma função de detecção e sai com exit code 0 (true) ou 1 (false).",
    )
    parser.add_argument(
        "--detect-arg",
        help="Argumento extra para --detect (ex: nome do repositório para has_debian_repo).",
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="Não solicitar elevação de privilégios automaticamente (Windows).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Mostra output de comandos capturados e tracebacks completos.",
    )
    args = parser.parse_args()

    global _DEBUG
    if args.debug:
        _DEBUG = True

    # --detect: executa função de detecção e sai com exit code apropriado
    if args.detect:
        func, num_args = _DETECT_FUNCTIONS[args.detect]
        if num_args > 0:
            if not args.detect_arg:
                print(
                    f"Erro: --detect {args.detect} requer --detect-arg",
                    file=sys.stderr,
                )
                sys.exit(2)
            result = func(args.detect_arg)
        else:
            result = func()
        sys.exit(0 if result else 1)

    if not args.task:
        interactive_menu()
        return

    if args.task == "all":
        run_all()
    else:
        TASKS[args.task]()



if __name__ == "__main__":
    try:
        try:
            main()
        except KeyboardInterrupt:
            print("\nCancelado pelo usuário.")
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            print(f"\nErro ao executar comando: {e}", file=sys.stderr)
            if _DEBUG:
                import traceback
                traceback.print_exc()
            sys.exit(e.returncode)
        except Exception as e:
            print(f"\nErro inesperado: {e}", file=sys.stderr)
            if _DEBUG:
                import traceback
                traceback.print_exc()
            else:
                print("Use --debug para ver o traceback completo.", file=sys.stderr)
            sys.exit(1)
    finally:
        if IS_WINDOWS and not _SKIP_PAUSE:
            pause()


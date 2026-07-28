# bootstrap

Scripts de configuração inicial para máquinas que rodam o Fish4.

## setup-tools.py

Instalador/configurador multiplataforma (Windows 11 e Linux Debian/Ubuntu/AlmaLinux).

### Uso rápido

**Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/carlosrenatosds/bootstrap/main/setup-tools.py -o setup-tools.py
python3 setup-tools.py menu
```

**Windows (PowerShell):**
```powershell
iwr -Uri "https://raw.githubusercontent.com/carlosrenatosds/bootstrap/main/setup-tools.py" -OutFile "setup-tools.py"; python setup-tools.py menu
```

> Pré-requisito: Python 3 instalado. No Debian já vem. No Windows: Microsoft Store ou `winget install Python.Python.3`.

### Tarefas disponíveis

| Comando | Descrição |
|---|---|
| `menu` | Menu interativo com navegação por setas |
| `packages` | Instalar pacotes básicos sugeridos |
| `laptop` | Configurações para laptops (Linux) |
| `syncthing-install` | Instalar o Syncthing e iniciar o serviço |
| `syncthing-setup` | Configurar o Syncthing (pasta BUILDS) |
| `tailscale` | Instalar e configurar o Tailscale |
| `aws-install` | Instalar o AWS CLI |
| `aws` | Configurar credenciais AWS (access key, região) |
| `aws-test` | Testar a configuração do AWS CLI |
| `gtk` | Customizar o GNOME para melhorar o uso do Fish4 (Linux) |
| `extensions` | Instalar extensões GNOME (Linux) |
| `extensions-enable` | Ativar extensões GNOME (Linux) |
| `keyd` | Forçar ENTER e , no teclado numérico (Linux) |
| `cups` | Configuração do CUPS (Linux) |
| `cups-on` / `cups-off` | Liberar/Pausar filas de impressão (Linux) |
| `printers-add` / `printers-del` | Adicionar/Remover impressoras Fish4 (Linux) |
| `all` | Executar todas as tarefas em ordem |

### Tailscale

Para usar o Tailscale, copie o arquivo de exemplo e edite com sua chave:

```bash
cp .setup-tools.env.example .setup-tools.env
# edite .setup-tools.env com a chave real
```

Sem o arquivo, o script pede a chave interativamente.

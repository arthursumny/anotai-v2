"""Registro único dos provedores de integração Nango.

Fonte da verdade, pura e sem I/O de rede/banco, para responder: quais
aplicações um usuário pode conectar e o que uma automação pode fazer com cada
uma hoje. Backend, UI (`apps/web/src/pages/Integrations.tsx`) e documentação
(`docs/operacao-integracoes.md`) espelham este registro — para adicionar um
provedor novo, comece por aqui (ver "Como adicionar um novo provedor" no
runbook de integrações).

`provider` é exatamente o valor gravado em `IntegrationConnection.provider`
(CHECK `ck_integration_connection_provider`). `config_key_setting` é o NOME do
atributo de `settings` que identifica a config de integração no Nango — este
módulo só lê o valor por nome; nenhuma credencial mora aqui ou no banco.

Status possíveis:
- ``active``: há código de execução ligado hoje (ação de automação real);
- ``config_required``: o caminho de código existe, mas falta a chave de config
  do Nango e/ou um tipo de ação para usá-lo;
- ``mapping_only``: declarado apenas para mapeamento — nenhuma ação de
  automação executa este provedor hoje.
"""

from dataclasses import dataclass

from app.core.config import settings

STATUS_ACTIVE = "active"
STATUS_CONFIG_REQUIRED = "config_required"
STATUS_MAPPING_ONLY = "mapping_only"

PROVIDER_STATUSES = (STATUS_ACTIVE, STATUS_CONFIG_REQUIRED, STATUS_MAPPING_ONLY)


@dataclass(frozen=True)
class ProviderDefinition:
    """Descreve um provedor conectável e o que ele permite hoje."""

    # Valor de IntegrationConnection.provider (espelhado no CHECK do banco).
    provider: str
    # NOME do atributo de settings com a chave de config da integração no Nango.
    config_key_setting: str
    # Rótulo de exibição (pt-BR).
    label: str
    # Descrição de uma linha (pt-BR).
    description: str
    # O que UMA AUTOMAÇÃO pode fazer com este provedor HOJE (pt-BR).
    capabilities: tuple[str, ...]
    # Escopo OAuth que a config de integração no Nango precisa pedir (pt-BR).
    oauth_scope_note: str
    # active | config_required | mapping_only (ver docstring do módulo).
    status: str

    @property
    def required_settings(self) -> tuple[str, ...]:
        """Atributos de `settings` que precisam estar preenchidos para configurar."""
        return (self.config_key_setting,)


PROVIDERS: dict[str, ProviderDefinition] = {definition.provider: definition for definition in (
    ProviderDefinition(
        provider="google_gmail",
        config_key_setting="NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY",
        label="Gmail",
        description=(
            "Lê e-mails recebidos (com opt-in) e envia e-mails das automações "
            "pela própria conta conectada do usuário."
        ),
        capabilities=(
            "Ler e-mails recebidos e acompanhar a caixa de entrada (opt-in separado, com retenção)",
            "Enviar e-mails das automações a partir da conta Gmail conectada (SEND_EMAIL)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango precisa pedir um escopo que permita ENVIAR — "
            "ex.: https://www.googleapis.com/auth/gmail.send (ou gmail.modify) junto de "
            "https://www.googleapis.com/auth/gmail.readonly. Somente gmail.readonly não envia: "
            "sem o escopo de envio, a ação SEND_EMAIL falha em runtime."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="google_calendar",
        config_key_setting="NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY",
        label="Google Agenda",
        description="Cria compromissos na sua agenda a partir das suas notas, com confirmação.",
        capabilities=(
            "Criar eventos no calendário `primary` da conta conectada (CREATE_CALENDAR_EVENT)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango deve pedir "
            "https://www.googleapis.com/auth/calendar.events."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="google_sheets",
        config_key_setting="NANGO_GOOGLE_SHEETS_PROVIDER_CONFIG_KEY",
        label="Google Planilhas",
        description=(
            "Anexa linhas a uma planilha a partir das suas notas, via execução "
            "dinâmica de ferramentas."
        ),
        capabilities=(
            "Anexar uma linha a uma aba de planilha (append_google_sheet_row)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango precisa pedir "
            "https://www.googleapis.com/auth/spreadsheets (e "
            "https://www.googleapis.com/auth/drive.file para arquivos criados no Anotaí)."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="google_tasks",
        config_key_setting="NANGO_GOOGLE_TASKS_PROVIDER_CONFIG_KEY",
        label="Google Tasks",
        description=(
            "Cria tarefas na sua lista padrão a partir das notas, via execução "
            "dinâmica de ferramentas."
        ),
        capabilities=(
            "Criar uma tarefa na lista padrão da conta conectada (create_google_task)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango precisa pedir "
            "https://www.googleapis.com/auth/tasks."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="google_drive",
        config_key_setting="NANGO_GOOGLE_DRIVE_PROVIDER_CONFIG_KEY",
        label="Google Drive",
        description=(
            "Cria arquivos no seu Drive a partir das notas, via execução dinâmica "
            "de ferramentas."
        ),
        capabilities=(
            "Criar um arquivo no Drive da conta conectada (create_google_drive_file)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango precisa pedir "
            "https://www.googleapis.com/auth/drive.file (só arquivos criados no Anotaí)."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="notion",
        config_key_setting="NANGO_NOTION_PROVIDER_CONFIG_KEY",
        label="Notion",
        description=(
            "Cria uma página num database do Notion a partir da nota gerada."
        ),
        capabilities=(
            "Criar página no database configurado no modelo, preenchendo as "
            "propriedades que você mapeou (CREATE_NOTION_PAGE)",
        ),
        oauth_scope_note=(
            "OAuth próprio do Notion (não é escopo Google): conceda acesso à página "
            "ou database que a automação poderá usar, senão a criação da página "
            "falha em runtime."
        ),
        status=STATUS_ACTIVE,
    ),
    ProviderDefinition(
        provider="slack",
        config_key_setting="NANGO_SLACK_PROVIDER_CONFIG_KEY",
        label="Slack",
        description=(
            "Envia uma mensagem para um canal do Slack escolhido por você, pela "
            "própria conta Slack conectada — o canal é configurado no template e "
            "nunca vem do conteúdo gerado."
        ),
        capabilities=(
            "Enviar uma mensagem num canal do Slack (SEND_SLACK_MESSAGE)",
        ),
        oauth_scope_note=(
            "A config de integração no Nango precisa do escopo de escrita do Slack "
            "(chat:write) para o método chat.postMessage. O remetente (app ou bot) "
            "também precisa ser membro do canal de destino — sem isso o Slack "
            "responde ok=false (ex.: not_in_channel)."
        ),
        status=STATUS_ACTIVE,
    ),
)}


def get_provider(provider: str) -> ProviderDefinition | None:
    """Retorna a definição do provedor ou None se não estiver no registro."""
    return PROVIDERS.get(provider)


def all_providers() -> list[ProviderDefinition]:
    """Todos os provedores na ordem do registro (ordem de exibição)."""
    return list(PROVIDERS.values())


def setting_value(setting_name: str) -> str:
    """Lê um atributo de `settings` pelo NOME; desconhecido/vazio vira ""."""
    value = getattr(settings, setting_name, "")
    return value.strip() if isinstance(value, str) else ""


def config_key(provider: str) -> str:
    """Chave de configuração do Nango para o provedor ("" se não configurada)."""
    definition = get_provider(provider)
    return setting_value(definition.config_key_setting) if definition else ""


def missing_settings(provider: str) -> list[str]:
    """Nomes de `settings` obrigatórios e ainda vazios para o provedor."""
    definition = get_provider(provider)
    if definition is None:
        return []
    return [name for name in definition.required_settings if not setting_value(name)]


def is_configured(provider: str) -> bool:
    """True quando TODAS as configurações obrigatórias do provedor estão preenchidas."""
    definition = get_provider(provider)
    return definition is not None and not missing_settings(provider)


def configured_providers() -> list[str]:
    """Provedores com todas as configurações obrigatórias preenchidas (em ordem)."""
    return [definition.provider for definition in all_providers() if is_configured(definition.provider)]

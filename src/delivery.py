"""Envio do report pelos canais: SMTP, NS (serviço de notificações da Multicert, SOAP) e Telegram."""

import base64
import smtplib
from datetime import datetime
from email.message import EmailMessage
from xml.sax.saxutils import escape

import requests

import db


class DeliveryError(Exception):
    pass


CHANNEL_KINDS = {'smtp': 'E-mail (SMTP)', 'ns': 'E-mail (NS / SOAP)', 'telegram': 'Telegram'}

# Campos de cada tipo de canal: (nome, rótulo, tipo do input, obrigatório)
CHANNEL_FIELDS = {
    'smtp': [('host', 'Servidor', 'text', True), ('port', 'Porta', 'number', True),
             ('starttls', 'STARTTLS', 'checkbox', False), ('username', 'Utilizador (opcional)', 'text', False),
             ('sender', 'Remetente', 'email', True)],
    'ns': [('endpoint', 'Endpoint SOAP', 'url', True), ('sender', 'Remetente (From)', 'email', True),
           ('client_id', 'ClientID', 'text', True), ('system_name', 'SystemName', 'text', True),
           ('client_name', 'ClientName', 'text', True), ('template_id', 'TemplateID', 'text', True),
           ('sign_message', 'Assinar e-mail (SignMessage)', 'checkbox', False),
           ('high_priority', 'Prioridade alta (HighPriority)', 'checkbox', False),
           ('verify_tls', 'Verificar certificado TLS', 'checkbox', False)],
    'telegram': [('api_url', 'URL da API', 'url', True), ('proxy', 'Proxy (opcional)', 'url', False)],
}
# Segredo de cada tipo (cifrado, nunca devolvido ao browser)
CHANNEL_SECRET = {'smtp': 'Password (opcional)', 'ns': None, 'telegram': 'Token do bot'}


def recipients_text(kind, target):
    if kind == 'telegram':
        topic = f" · tópico {target['topic_id']}" if target.get('topic_id') else ''
        return f"chat {target.get('chat_id')}{topic}"
    cc = f" (cc: {', '.join(target['cc'])})" if target.get('cc') else ''
    return ', '.join(target.get('to', [])) + cc


def send(channel, target, subject, message, file_name, mime, data):
    """Envia o ficheiro; devolve uma descrição curta para o log da execução."""
    config = channel['config']
    secret = db.decrypt(channel['secret'])
    try:
        if channel['kind'] == 'smtp':
            return _smtp(config, secret, target, subject, message, file_name, mime, data)
        if channel['kind'] == 'ns':
            return _ns(config, target, subject, message, file_name, mime, data)
        if channel['kind'] == 'telegram':
            return _telegram(config, secret, target, subject, message, file_name, data)
    except DeliveryError:
        raise
    except Exception as exc:
        # Os erros de rede do requests trazem o URL, e o do Telegram tem o token do bot
        text = f'{type(exc).__name__}: {exc}'
        raise DeliveryError((text.replace(secret, '***') if secret else text)[:300]) from exc
    raise DeliveryError(f"Tipo de canal desconhecido: {channel['kind']}")


def _smtp(config, password, target, subject, message, file_name, mime, data):
    to, cc = target.get('to', []), target.get('cc', [])
    if not to:
        raise DeliveryError('Sem destinatários.')
    mail = EmailMessage()
    mail['From'] = config['sender']
    mail['To'] = ', '.join(to)
    if cc:
        mail['Cc'] = ', '.join(cc)
    mail['Subject'] = subject
    mail.set_content(message or 'Report em anexo.')
    maintype, subtype = mime.split('/', 1)
    mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=file_name)
    with smtplib.SMTP(config['host'], int(config.get('port') or 25), timeout=60) as server:
        if config.get('starttls'):
            server.starttls()
        if config.get('username'):
            server.login(config['username'], password)
        server.send_message(mail)
    return f'enviado para {recipients_text("smtp", target)}'


def ns_request(config, to, subject, message, file_name, mime, data):
    """Envelope ``MailRequest`` do NS (o mesmo dos scripts antigos), com o anexo em base64."""
    flag = lambda key: 'true' if config.get(key) else 'false'  # noqa: E731
    return f'''<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:mes="http://messages.ns.project.multicert.com" xmlns:com="http://commons.messages.ns.project.multicert.com">
  <soapenv:Header/>
  <soapenv:Body>
    <mes:MailRequest>
      <mes:Header>
        <com:Version>1.0</com:Version>
        <com:RequestDate>{datetime.now().astimezone().isoformat(timespec='seconds')}</com:RequestDate>
        <com:ClientID>{escape(config['client_id'])}</com:ClientID>
      </mes:Header>
      <mes:From>{escape(config['sender'])}</mes:From>
      <mes:To>{escape(to)}</mes:To>
      <mes:Subject>{escape(subject)}</mes:Subject>
      <mes:Body>{escape(message or 'Report em anexo.')}</mes:Body>
      <mes:SignMessage>{flag('sign_message')}</mes:SignMessage>
      <mes:HighPriority>{flag('high_priority')}</mes:HighPriority>
      <mes:SystemName>{escape(config['system_name'])}</mes:SystemName>
      <mes:ClientName>{escape(config['client_name'])}</mes:ClientName>
      <mes:TemplateID>{escape(config['template_id'])}</mes:TemplateID>
      <mes:Attachments>
        <com:MimeType>{escape(mime)}</com:MimeType>
        <com:File>{base64.b64encode(data).decode()}</com:File>
        <com:Name>{escape(file_name)}</com:Name>
      </mes:Attachments>
    </mes:MailRequest>
  </soapenv:Body>
</soapenv:Envelope>'''


def _ns(config, target, subject, message, file_name, mime, data):
    # O MailRequest leva um único To: um pedido por destinatário (cc incluídos)
    addresses = target.get('to', []) + target.get('cc', [])
    if not addresses:
        raise DeliveryError('Sem destinatários.')
    for address in addresses:
        response = requests.post(config['endpoint'], data=ns_request(config, address, subject, message, file_name,
                                                                      mime, data).encode(),
                                 headers={'Content-Type': 'text/xml; charset=utf-8'},
                                 verify=bool(config.get('verify_tls')), timeout=60)
        if response.status_code != 200 or 'Fault>' in response.text:
            raise DeliveryError(f'NS respondeu HTTP {response.status_code} para {address}: {response.text[:200]}')
    return f'enviado pelo NS para {", ".join(addresses)}'


def _telegram(config, token, target, subject, message, file_name, data):
    if not token:
        raise DeliveryError('O canal não tem o token do bot.')
    payload = {'chat_id': target['chat_id'], 'caption': f'{subject}\n{message}'.strip()[:1024]}
    if target.get('topic_id'):
        payload['message_thread_id'] = target['topic_id']
    proxies = {'http': config['proxy'], 'https': config['proxy']} if config.get('proxy') else None
    response = requests.post(f"{config['api_url'].rstrip('/')}/bot{token}/sendDocument", data=payload,
                             files={'document': (file_name, data)}, proxies=proxies, timeout=120)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not body.get('ok'):
        # A descrição do Telegram nunca leva o token; o URL sim, por isso não se mostra
        raise DeliveryError(f"Telegram: {body.get('description') or f'HTTP {response.status_code}'}")
    return f'enviado para o Telegram ({recipients_text("telegram", target)})'

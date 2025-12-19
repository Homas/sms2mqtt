# SMS-MQTT Bridge for Huawei LTE Routers

A Python script that pulls SMS messages from a Huawei LTE router via REST API and forwards new messages to an MQTT broker. Designed to run as a scheduled CRON task.

## Features

- Retrieves SMS messages from Huawei router API
- Tracks sent messages to avoid duplicates
- Forwards new messages to MQTT in chronological order
- Handles authentication errors with retry logic
- Rate-limited error notifications (1 per hour max)
- Full UTF-8/Cyrillic support

## Configuration

Edit the variables at the top of `sms_mqtt_bridge.py`:

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_BROKER_IP` | MQTT broker IP address | `192.168.42.225` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MQTT_TOPIC_SMS` | Topic for SMS messages | `sms/inbox` |
| `MQTT_TOPIC_ERROR` | Topic for error notifications | `sms/error` |
| `ROUTER_SMS_API_URL` | Router SMS API endpoint | `http://192.168.142.1/api/sms/sms-list` |
| `ROUTER_AUTH_URL` | Router auth URL | `http://192.168.142.1/html/smsinbox.html` |
| `TRACKER_FILE` | Path to sent message tracker | `tracker.json` |
| `SMS_LOG_FILE` | Path to SMS activity log | `sms.log` |
| `ERROR_LOG_FILE` | Path to error log | `error.log` |
| `MAX_AUTH_RETRIES` | Max authentication retries | `3` |
| `ERROR_THROTTLE_SECONDS` | Min seconds between error notifications | `3600` |
| `SMS_READ_COUNT` | Number of messages to fetch per request | `50` |
| `MQTT_SEND_DELAY_SECONDS` | Delay between MQTT messages (rate limiting) | `1.0` |
| `MQTT_BATCH_LIMIT` | Max messages per execution (0 = unlimited) | `20` |

## Dependencies

```bash
pip install requests paho-mqtt
```

## Usage

Run manually:
```bash
python3 sms_mqtt_bridge.py
```

Schedule via CRON (every 5 minutes):
```cron
*/5 * * * * /usr/bin/python3 /path/to/sms_mqtt_bridge.py
```

## MQTT Message Format

```json
{
  "forwarded_at": "2025-12-18T10:30:00Z",
  "sms_timestamp": "2025-12-17 17:25:03",
  "phone": "+1234567890",
  "content": "Your message text here"
}
```

## Home Assistant Integration

### MQTT Sensors

```yaml
mqtt:
  sensor:
    - name: "Last SMS"
      state_topic: "sms/inbox"
      value_template: "{{ value_json.phone }}"
      json_attributes_topic: "sms/inbox"
      json_attributes_template: >
        {{ value_json | tojson }}

    - name: "SMS Bridge Error"
      state_topic: "sms/error"
      value_template: "{{ value_json.error[:50] }}"
      json_attributes_topic: "sms/error"
      json_attributes_template: >
        {{ value_json | tojson }}
```

### Telegram Notification Automation

```yaml
automation:
  - alias: "Forward SMS to Telegram"
    trigger:
      - platform: mqtt
        topic: "sms/inbox"
    action:
      - service: telegram_bot.send_message
        data:
          target: !secret telegram_chat_id
          message: >
            📱 <b>SMS from {{ trigger.payload_json.phone }}</b>

            {{ trigger.payload_json.content }}

            <i>Received: {{ trigger.payload_json.sms_timestamp }}</i>
          parse_mode: html

  - alias: "SMS Bridge Error Alert"
    trigger:
      - platform: mqtt
        topic: "sms/error"
    action:
      - service: telegram_bot.send_message
        data:
          target: !secret telegram_chat_id
          message: "⚠️ <b>SMS Bridge Error:</b> {{ trigger.payload_json.error }}"
          parse_mode: html
```

## License

MIT

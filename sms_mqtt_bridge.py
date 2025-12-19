#!/usr/bin/env python3
"""
SMS-MQTT Bridge Script

Pulls SMS messages from a Huawei LTE router via REST API and forwards
new messages to an MQTT broker. Designed to run as a scheduled CRON task.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import os
import time
import xml.etree.ElementTree as ET

# =============================================================================
# CONFIGURATION
# =============================================================================

# MQTT Broker Settings
MQTT_BROKER_IP = "192.168.42.225"
MQTT_PORT = 1883
MQTT_TOPIC_SMS = "sms/inbox"
MQTT_TOPIC_ERROR = "sms/error"

# Huawei Router API Settings
ROUTER_SMS_API_URL = "http://192.168.142.1/api/sms/sms-list"
ROUTER_AUTH_URL = "http://192.168.142.1/html/smsinbox.html"

# File Paths
TRACKER_FILE = "tracker.json"
SMS_LOG_FILE = "sms.log"
ERROR_LOG_FILE = "error.log"
THROTTLE_FILE = "throttle.json"

# Retry and Throttle Settings
MAX_AUTH_RETRIES = 3
ERROR_THROTTLE_SECONDS = 3600  # 1 hour

# SMS Retrieval Settings
SMS_READ_COUNT = 50  # Number of messages to fetch per request

# MQTT Rate Limiting (to prevent Telegram bot rate limits)
# Telegram limits: ~1 msg/sec per chat, ~20 msg/min for groups
MQTT_SEND_DELAY_SECONDS = 1.0  # Delay between MQTT messages (seconds)
MQTT_BATCH_LIMIT = 20  # Max messages per execution (0 = unlimited)


# =============================================================================
# DATA MODELS
# =============================================================================

@dataclass
class SMSMessage:
    """Represents an SMS message retrieved from the router."""
    index: str      # Unique SMS ID from router
    phone: str      # Sender phone number or name
    content: str    # Message content (UTF-8)
    date: str       # Timestamp in "YYYY-MM-DD HH:MM:SS" format


# =============================================================================
# XML PARSER
# =============================================================================

def parse_sms_response(xml_string: str) -> list[SMSMessage]:
    """
    Parses XML response from Huawei router and extracts SMS messages.
    
    Args:
        xml_string: XML string from router SMS API
        
    Returns:
        List of SMSMessage objects extracted from the XML
    """
    messages = []
    root = ET.fromstring(xml_string)
    
    for message_elem in root.findall('.//Message'):
        index = message_elem.findtext('Index', default='')
        phone = message_elem.findtext('Phone', default='')
        content = message_elem.findtext('Content', default='')
        date = message_elem.findtext('Date', default='')
        
        messages.append(SMSMessage(
            index=index,
            phone=phone,
            content=content,
            date=date
        ))
    
    return messages


def is_auth_error(xml_string: str) -> bool:
    """
    Checks if XML response contains authentication error code 100003.
    
    Args:
        xml_string: XML string from router API
        
    Returns:
        True if error code 100003 is present, False otherwise
    """
    try:
        root = ET.fromstring(xml_string)
        error_code = root.findtext('.//code')
        return error_code == '100003'
    except ET.ParseError:
        return False


# =============================================================================
# SMS TRACKER
# =============================================================================

class SMSTracker:
    """
    Manages persistent tracking of sent SMS messages.
    Tracks which SMS indices have been forwarded to MQTT to avoid duplicates.
    """
    
    def __init__(self, tracker_file: str):
        """
        Initialize the SMS tracker.
        
        Args:
            tracker_file: Path to the JSON file for persistent storage
        """
        self.tracker_file = tracker_file
        self.sent_indices: set[str] = set()
    
    def load(self) -> None:
        """
        Loads sent indices from the tracker file.
        Creates an empty tracker if the file doesn't exist.
        """
        if not os.path.exists(self.tracker_file):
            self.sent_indices = set()
            return
        
        try:
            with open(self.tracker_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.sent_indices = set(data.get('sent_indices', []))
        except (json.JSONDecodeError, IOError):
            self.sent_indices = set()
    
    def save(self) -> None:
        """
        Saves sent indices to the tracker file.
        """
        data = {'sent_indices': list(self.sent_indices)}
        with open(self.tracker_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    
    def is_new(self, index: str) -> bool:
        """
        Checks if an SMS index is new (not yet sent).
        
        Args:
            index: The SMS index to check
            
        Returns:
            True if the index hasn't been sent, False otherwise
        """
        return index not in self.sent_indices
    
    def mark_sent(self, index: str) -> None:
        """
        Marks an SMS index as sent.
        
        Args:
            index: The SMS index to mark as sent
        """
        self.sent_indices.add(index)
    
    def filter_new_messages(self, messages: list[SMSMessage]) -> list[SMSMessage]:
        """
        Filters messages to only include new ones, sorted by date ascending.
        
        Args:
            messages: List of SMS messages to filter
            
        Returns:
            List of new messages sorted by date in ascending order
        """
        new_messages = [msg for msg in messages if self.is_new(msg.index)]
        return sorted(new_messages, key=lambda msg: msg.date)


# =============================================================================
# LOGGER
# =============================================================================

class Logger:
    """
    Handles logging of SMS forwarding activity and errors to files.
    """
    
    def __init__(self, sms_log_file: str, error_log_file: str):
        """
        Initialize the logger.
        
        Args:
            sms_log_file: Path to the SMS activity log file
            error_log_file: Path to the error log file
        """
        self.sms_log_file = sms_log_file
        self.error_log_file = error_log_file
    
    def log_sms(self, message: SMSMessage, forwarded_at: str) -> None:
        """
        Logs SMS forwarding to the SMS log file.
        
        Format: timestamp | SMS_DATE: date | FROM: phone | CONTENT: content
        
        Args:
            message: The SMS message that was forwarded
            forwarded_at: ISO timestamp of when the message was forwarded
        """
        log_entry = f"{forwarded_at} | SMS_DATE: {message.date} | FROM: {message.phone} | CONTENT: {message.content}\n"
        try:
            with open(self.sms_log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except IOError as e:
            print(f"Failed to write to SMS log: {e}", file=__import__('sys').stderr)
    
    def log_error(self, error: str) -> None:
        """
        Logs error to the error log file with timestamp.
        
        Format: timestamp | ERROR: error_details
        
        Args:
            error: The error details to log
        """
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        log_entry = f"{timestamp} | ERROR: {error}\n"
        try:
            with open(self.error_log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except IOError as e:
            print(f"Failed to write to error log: {e}", file=__import__('sys').stderr)


# =============================================================================
# ERROR HANDLER
# =============================================================================

# =============================================================================
# MQTT PUBLISHER
# =============================================================================

class MQTTPublisher:
    """
    Handles publishing SMS messages and error notifications to MQTT broker.
    """
    
    def __init__(self, broker_ip: str, port: int):
        """
        Initialize the MQTT publisher.
        
        Args:
            broker_ip: IP address of the MQTT broker
            port: Port number of the MQTT broker
        """
        self.broker_ip = broker_ip
        self.port = port
    
    def publish_sms(self, message: SMSMessage, topic: str) -> bool:
        """
        Publishes an SMS message to MQTT.
        
        Message format:
        {
            "forwarded_at": "ISO timestamp",
            "sms_timestamp": "original date",
            "phone": "sender",
            "content": "message text"
        }
        
        Args:
            message: The SMS message to publish
            topic: The MQTT topic to publish to
            
        Returns:
            True if publish was successful, False otherwise
        """
        import paho.mqtt.client as mqtt
        
        forwarded_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        
        payload = {
            "forwarded_at": forwarded_at,
            "sms_timestamp": message.date,
            "phone": message.phone,
            "content": message.content
        }
        
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.connect(self.broker_ip, self.port, keepalive=60)
            result = client.publish(topic, json.dumps(payload, ensure_ascii=False))
            client.disconnect()
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            return False
    
    def publish_error(self, error_message: str, topic: str) -> bool:
        """
        Publishes an error notification to MQTT.
        
        Args:
            error_message: The error message to publish
            topic: The MQTT topic to publish to
            
        Returns:
            True if publish was successful, False otherwise
        """
        import paho.mqtt.client as mqtt
        
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        
        payload = {
            "timestamp": timestamp,
            "error": error_message
        }
        
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.connect(self.broker_ip, self.port, keepalive=60)
            result = client.publish(topic, json.dumps(payload, ensure_ascii=False))
            client.disconnect()
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            return False


# =============================================================================
# ERROR HANDLER
# =============================================================================

# =============================================================================
# SMS RETRIEVER
# =============================================================================

class SMSRetriever:
    """
    Handles communication with the Huawei router SMS API.
    Retrieves SMS messages and handles authentication.
    """
    
    def __init__(self, api_url: str, auth_url: str):
        """
        Initialize the SMS retriever.
        
        Args:
            api_url: URL for the SMS retrieval API
            auth_url: URL for authentication (smsinbox.html)
        """
        self.api_url = api_url
        self.auth_url = auth_url
    
    def fetch_messages(self) -> tuple[bool, str]:
        """
        Fetches SMS messages from the router.
        
        Sends a POST request with XML payload requesting up to SMS_READ_COUNT
        messages sorted by date.
        
        Returns:
            Tuple of (success, xml_response_or_error)
            - On success: (True, xml_response_string)
            - On failure: (False, error_message)
        """
        import requests
        
        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<request>
<PageIndex>1</PageIndex>
<ReadCount>{SMS_READ_COUNT}</ReadCount>
<BoxType>1</BoxType>
<SortType>0</SortType>
<Ascending>0</Ascending>
<UnreadPreferred>0</UnreadPreferred>
</request>"""
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        }
        
        try:
            response = requests.post(
                self.api_url,
                data=xml_payload,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            # Force UTF-8 decoding for Russian/Cyrillic content
            response.encoding = 'utf-8'
            return (True, response.text)
        except requests.exceptions.Timeout:
            return (False, "Connection to router timed out after 30s")
        except requests.exceptions.ConnectionError:
            return (False, "Failed to connect to router")
        except requests.exceptions.RequestException as e:
            return (False, f"Request failed: {str(e)}")
    
    def authenticate(self) -> bool:
        """
        Performs authentication by accessing smsinbox.html.
        
        This triggers the router's session initialization which is required
        when error code 100003 is received.
        
        Returns:
            True if authentication request was successful, False otherwise
        """
        import requests
        
        try:
            response = requests.get(self.auth_url, timeout=30)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException:
            return False
    
    def fetch_with_auth_retry(
        self,
        error_handler: 'ErrorHandler',
        publisher: 'MQTTPublisher',
        logger: 'Logger',
        max_retries: int = MAX_AUTH_RETRIES
    ) -> tuple[bool, str]:
        """
        Fetches messages with automatic authentication retry on error 100003.
        
        If the router returns error code 100003, attempts authentication
        and retries the request up to max_retries times.
        
        Args:
            error_handler: ErrorHandler instance for throttled error notifications
            publisher: MQTTPublisher instance for sending error notifications
            logger: Logger instance for logging errors
            max_retries: Maximum number of authentication retry attempts
            
        Returns:
            Tuple of (success, xml_response_or_error)
            - On success: (True, xml_response_string)
            - On failure: (False, error_message)
        """
        success, response = self.fetch_messages()
        
        if not success:
            logger.log_error(response)
            return (False, response)
        
        retry_count = 0
        while is_auth_error(response) and retry_count < max_retries:
            retry_count += 1
            logger.log_error(f"Authentication error (100003), attempt {retry_count}/{max_retries}")
            
            if not self.authenticate():
                logger.log_error(f"Authentication attempt {retry_count} failed")
                continue
            
            success, response = self.fetch_messages()
            if not success:
                logger.log_error(response)
                return (False, response)
        
        if is_auth_error(response):
            error_msg = f"Authentication failed after {max_retries} retries"
            logger.log_error(error_msg)
            
            if error_handler.can_send_error():
                publisher.publish_error(error_msg, MQTT_TOPIC_ERROR)
                error_handler.record_error_sent()
            
            return (False, error_msg)
        
        return (True, response)


class ErrorHandler:
    """
    Manages error handling with throttled notifications.
    Ensures error notifications to MQTT are rate-limited to prevent spam.
    """
    
    def __init__(self, throttle_file: str, throttle_seconds: int):
        """
        Initialize the error handler.
        
        Args:
            throttle_file: Path to the JSON file storing last error notification timestamp
            throttle_seconds: Minimum seconds between error notifications
        """
        self.throttle_file = throttle_file
        self.throttle_seconds = throttle_seconds
        self._last_notification: datetime | None = None
        self._load_throttle_state()
    
    def _load_throttle_state(self) -> None:
        """
        Loads the last error notification timestamp from the throttle file.
        """
        if not os.path.exists(self.throttle_file):
            self._last_notification = None
            return
        
        try:
            with open(self.throttle_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                timestamp_str = data.get('last_error_notification')
                if timestamp_str:
                    self._last_notification = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                else:
                    self._last_notification = None
        except (json.JSONDecodeError, IOError, ValueError):
            self._last_notification = None
    
    def can_send_error(self) -> bool:
        """
        Checks if enough time has passed since the last error notification.
        
        Returns:
            True if outside the throttle window (can send), False otherwise
        """
        if self._last_notification is None:
            return True
        
        now = datetime.utcnow()
        # Handle timezone-aware vs naive datetime comparison
        last_notification_naive = self._last_notification.replace(tzinfo=None) if self._last_notification.tzinfo else self._last_notification
        elapsed = (now - last_notification_naive).total_seconds()
        return elapsed >= self.throttle_seconds
    
    def record_error_sent(self) -> None:
        """
        Records the current timestamp as the last error notification time.
        Saves to the throttle file for persistence across script executions.
        """
        now = datetime.utcnow()
        self._last_notification = now
        
        data = {'last_error_notification': now.strftime('%Y-%m-%dT%H:%M:%SZ')}
        try:
            with open(self.throttle_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"Failed to write throttle file: {e}", file=__import__('sys').stderr)


# =============================================================================
# MAIN CONTROLLER
# =============================================================================

def main() -> int:
    """
    Main controller function for the SMS-MQTT bridge.
    
    Orchestrates the entire SMS retrieval and forwarding process:
    1. Initialize all components
    2. Fetch SMS messages with auth retry
    3. Filter new messages using tracker
    4. Publish each new message to MQTT in chronological order
    5. Log each sent message
    6. Mark messages as sent in tracker
    7. Save tracker state
    8. Exit with appropriate code
    
    Returns:
        Exit code: 0 on success, non-zero on fatal error
    
    Requirements: 7.1, 7.2, 7.3, 4.2, 5.2, 5.3, 5.4
    """
    import sys
    
    # Initialize components
    logger = Logger(SMS_LOG_FILE, ERROR_LOG_FILE)
    error_handler = ErrorHandler(THROTTLE_FILE, ERROR_THROTTLE_SECONDS)
    publisher = MQTTPublisher(MQTT_BROKER_IP, MQTT_PORT)
    tracker = SMSTracker(TRACKER_FILE)
    retriever = SMSRetriever(ROUTER_SMS_API_URL, ROUTER_AUTH_URL)
    
    def handle_fatal_error(error_msg: str) -> int:
        """
        Handles fatal errors with logging and throttled MQTT notification.
        
        Args:
            error_msg: The error message to log and potentially notify
            
        Returns:
            Non-zero exit code
        """
        logger.log_error(error_msg)
        
        # Send throttled error notification to MQTT
        if error_handler.can_send_error():
            publisher.publish_error(error_msg, MQTT_TOPIC_ERROR)
            error_handler.record_error_sent()
        
        return 1
    
    # Load tracker state
    try:
        tracker.load()
    except Exception as e:
        # Log but continue with empty tracker rather than failing
        logger.log_error(f"Failed to load tracker, starting fresh: {e}")
    
    # Fetch SMS messages with auth retry
    try:
        success, response = retriever.fetch_with_auth_retry(
            error_handler=error_handler,
            publisher=publisher,
            logger=logger,
            max_retries=MAX_AUTH_RETRIES
        )
    except Exception as e:
        return handle_fatal_error(f"Unexpected error during SMS retrieval: {e}")
    
    if not success:
        # Error already logged and potentially notified via MQTT in fetch_with_auth_retry
        return 1
    
    # Parse SMS messages from XML response
    try:
        messages = parse_sms_response(response)
    except ET.ParseError as e:
        return handle_fatal_error(f"Failed to parse SMS response (malformed XML): {e}")
    except Exception as e:
        return handle_fatal_error(f"Failed to parse SMS response: {e}")
    
    # Filter new messages and sort chronologically
    new_messages = tracker.filter_new_messages(messages)
    
    if not new_messages:
        # No new messages to process - success (Requirement 7.2)
        return 0
    
    # Apply batch limit to respect Telegram group rate limits (20 msg/min)
    if MQTT_BATCH_LIMIT > 0 and len(new_messages) > MQTT_BATCH_LIMIT:
        new_messages = new_messages[:MQTT_BATCH_LIMIT]
    
    # Process each new message in chronological order with rate limiting
    messages_sent = 0
    for i, message in enumerate(new_messages):
        # Rate limit: delay between messages (except before first)
        if i > 0 and MQTT_SEND_DELAY_SECONDS > 0:
            time.sleep(MQTT_SEND_DELAY_SECONDS)
        
        # Publish to MQTT
        forwarded_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        
        try:
            if publisher.publish_sms(message, MQTT_TOPIC_SMS):
                # Log successful send (Requirement 5.1)
                logger.log_sms(message, forwarded_at)
                # Mark as sent in tracker (Requirement 2.4)
                tracker.mark_sent(message.index)
                messages_sent += 1
            else:
                # Log MQTT publish failure but continue with next message
                logger.log_error(f"Failed to publish SMS {message.index} to MQTT")
        except Exception as e:
            # Log error but continue processing other messages
            logger.log_error(f"Error publishing SMS {message.index}: {e}")
    
    # Save tracker state (Requirement 2.5)
    try:
        tracker.save()
    except Exception as e:
        return handle_fatal_error(f"Failed to save tracker state: {e}")
    
    # Success - all new messages processed (Requirement 7.2)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

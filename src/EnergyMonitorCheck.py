#!/usr/bin/env python3
"""
Octopus Energy EV Charging Monitor
Monitors energy usage and sends SMS alerts when high usage indicates potential EV charging
"""

import requests
import json
from datetime import datetime, timedelta
import time
import logging
from twilio.rest import Client
import os
from typing import Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('energy_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class EnergyMonitor:
    def __init__(self, config: Dict):
        """Initialize the energy monitor with configuration"""
        self.config = config
        self.octopus_api_key = config['octopus_api_key']
        self.meter_mpan = config['meter_mpan']
        self.meter_serial = config['meter_serial']
        self.twilio_client = Client(
            config['twilio_account_sid'],
            config['twilio_auth_token']
        )
        self.twilio_from = config['twilio_from_number']
        self.twilio_to = config['twilio_to_number']

        # Thresholds
        self.high_usage_threshold = config.get('high_usage_threshold_kw', 30.0)  # kW
        self.sustained_minutes = config.get('sustained_minutes', 240)  # minutes
        self.baseline_usage = config.get('baseline_usage_kw', 0.5)  # kW normal usage

        # Alert management
        self.last_alert_time = None
        self.alert_cooldown = config.get('alert_cooldown_hours', 4) * 3600  # seconds

    def get_consumption_data(self, hours_back: int = 2) -> List[Dict]:
        """Get electricity consumption data from Octopus API"""
        try:
            # Calculate time range
            end_date = datetime.today() - timedelta(hours=24)
            end_time =  datetime(end_date.year, end_date.month, end_date.day)
            #end_time = datetime(2025,4,24)
            start_time = end_time - timedelta(hours=hours_back)

            # Format timestamps for API (ISO format)
            start_str = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')
            end_str = end_time.strftime('%Y-%m-%dT%H:%M:%SZ')

            # Octopus API endpoint
            url = f"https://api.octopus.energy/v1/electricity-meter-points/{self.meter_mpan}/meters/{self.meter_serial}/consumption/"

            params = {
                'period_from': start_str,
                'period_to': end_str,
                'order_by': 'period',
                'page_size': 200
            }

            # Make API request
            response = requests.get(
                url,
                params=params,
                auth=(self.octopus_api_key, ''),
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                return data.get('results', [])
            else:
                logger.error(f"Octopus API error: {response.status_code} - {response.text}")
                return []

        except Exception as e:
            logger.error(f"Error fetching consumption data: {e}")
            return []

    def analyze_usage_pattern(self, consumption_data: List[Dict]) -> Dict:
        """Analyze consumption data for EV charging patterns"""
        if not consumption_data:
            return {'is_charging': False, 'reason': 'No data available'}

        # Sort by timestamp (newest first from API)
        consumption_data.sort(key=lambda x: x['interval_start'], reverse=True)

        # Get recent readings (last 24 hour)
        recent_readings = consumption_data[:48]  # 30-min intervals, so 48 = 24 hours

        if len(recent_readings) < 2:
            return {'is_charging': False, 'reason': 'Insufficient data'}

        # Calculate average power for recent period
        total_consumption = sum(reading['consumption'] for reading in recent_readings)
        time_period_hours = len(recent_readings) * 0.5  # 30-minute intervals
        day_power_kw = (total_consumption / time_period_hours) * 24

        # Check for sustained high usage
        high_usage_count = sum(
            1 for reading in recent_readings
            if (reading['consumption'] / 0.5) > self.baseline_usage
        )

        # Determine if likely EV charging
        is_charging = (
                day_power_kw > self.high_usage_threshold and
                high_usage_count >= (self.sustained_minutes // 30)  # sustained for required time
        )

        analysis = {
            'is_charging': is_charging,
            'average_power_kw': round(day_power_kw, 2),
            'peak_power_kw': round(max(reading['consumption'] / 0.5 for reading in recent_readings), 2),
            'high_usage_periods': high_usage_count,
            'total_periods_analyzed': len(recent_readings),
            'latest_reading_time': recent_readings[0]['interval_start'] if recent_readings else None
        }

        if is_charging:
            analysis['reason'] = f"High sustained usage detected: {day_power_kw:.1f}kW average"
        else:
            analysis['reason'] = f"Usage within normal range: {day_power_kw:.1f}kW average"

        return analysis

    def send_alert(self, analysis: Dict):
        """Send SMS alert about potential EV charging"""
        try:
            # Check cooldown period
            current_time = time.time()
            if (self.last_alert_time and
                    current_time - self.last_alert_time < self.alert_cooldown):
                logger.info("Alert suppressed due to cooldown period")
                return

            # Compose message
            message = (
                f"🚗⚡ EV Charging Alert!\n\n"
                f"High energy usage detected at holiday property:\n"
                f"• Average: {analysis['average_power_kw']}kW\n"
                f"• Peak: {analysis['peak_power_kw']}kW\n"
                f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"This suggests someone may be charging an electric vehicle."
            )

            # Send SMS
            self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_from,
                to=self.twilio_to
            )

            self.last_alert_time = current_time
            logger.info(f"Alert sent successfully: {analysis['average_power_kw']}kW usage detected")

        except Exception as e:
            logger.error(f"Error sending alert: {e}")

    def run_check(self):
        """Run a single monitoring check"""
        logger.info("Starting energy usage check...")

        # Get recent consumption data
        consumption_data = self.get_consumption_data(hours_back=24)

        if not consumption_data:
            logger.warning("No consumption data received")
            return

        # Analyze for EV charging pattern
        analysis = self.analyze_usage_pattern(consumption_data)

        logger.info(f"Analysis: {analysis['reason']}")

        # Send alert if charging detected
        if analysis['is_charging']:
            self.send_alert(analysis)

        return analysis

    def run_continuous(self, check_interval_minutes: int = 30):
        """Run continuous monitoring"""
        logger.info(f"Starting continuous monitoring (checking every {check_interval_minutes} minutes)")

        while True:
            try:
                self.run_check()
                logger.info(f"Sleeping for {check_interval_minutes} minutes...")
                time.sleep(check_interval_minutes * 60)

            except KeyboardInterrupt:
                logger.info("Monitoring stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(300)  # Wait 5 minutes before retrying


def load_config() -> Dict:
    """Load configuration from environment variables or config file"""
    config = {
        # Octopus Energy API
        'octopus_api_key': os.getenv('OCTOPUS_API_KEY'),
        'meter_mpan': os.getenv('METER_MPAN'),
        'meter_serial': os.getenv('METER_SERIAL'),

        # Twilio
        'twilio_account_sid': os.getenv('TWILIO_ACCOUNT_SID'),
        'twilio_auth_token': os.getenv('TWILIO_AUTH_TOKEN'),
        'twilio_from_number': os.getenv('TWILIO_FROM_NUMBER'),
        'twilio_to_number': os.getenv('TWILIO_TO_NUMBER'),

        # Thresholds
        'high_usage_threshold_kw': float(os.getenv('HIGH_USAGE_THRESHOLD_KW', '30.0')),
        'sustained_minutes': int(os.getenv('SUSTAINED_MINUTES', '30')),
        'baseline_usage_kw': float(os.getenv('BASELINE_USAGE_KW', '0.8')),
        'alert_cooldown_hours': int(os.getenv('ALERT_COOLDOWN_HOURS', '4')),
    }

    # Validate required config
    required_keys = [
        'octopus_api_key', 'meter_mpan', 'meter_serial',
        'twilio_account_sid', 'twilio_auth_token',
        'twilio_from_number', 'twilio_to_number'
    ]

    missing_keys = [key for key in required_keys if not config.get(key)]
    if missing_keys:
        raise ValueError(f"Missing required configuration: {', '.join(missing_keys)}")

    return config


def main():
    """Main function"""
    try:
        # Load configuration
        config = load_config()

        # Create monitor instance
        monitor = EnergyMonitor(config)

        # Run check
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == '--once':
            # Run single check
            result = monitor.run_check()
            print(json.dumps(result, indent=2))
        else:
            # Run continuous monitoring
            monitor.run_continuous()

    except Exception as e:
        logger.error(f"Error in main: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
#!/usr/bin/env python3
"""
Tencent Cloud Resource Cleaner — SCF Handler
Version: 3.0.0

Automatically deletes expired cloud resources based on TTL tags.
Service-specific logic lives in the services/ package.
"""

import os
import json
import logging

from services import __version__, CLBCleaner, CBSCleaner, EIPCleaner, ENICleaner, HAVIPCleaner, SnapshotCleaner, NATCleaner, ASCleaner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = int(os.environ.get('DEFAULT_TTL_DAYS', '7'))
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
REGIONS = os.environ.get('REGIONS', '').split(',') if os.environ.get('REGIONS') else []

ENABLE_CLB = os.environ.get('ENABLE_CLB', 'true').lower() == 'true'
ENABLE_CBS = os.environ.get('ENABLE_CBS', 'true').lower() == 'true'
ENABLE_EIP = os.environ.get('ENABLE_EIP', 'true').lower() == 'true'
ENABLE_ENI = os.environ.get('ENABLE_ENI', 'true').lower() == 'true'
ENABLE_HAVIP = os.environ.get('ENABLE_HAVIP', 'true').lower() == 'true'
ENABLE_SNAPSHOT = os.environ.get('ENABLE_SNAPSHOT', 'true').lower() == 'true'
ENABLE_NAT = os.environ.get('ENABLE_NAT', 'true').lower() == 'true'
ENABLE_AS = os.environ.get('ENABLE_AS', 'true').lower() == 'true'

ALL_REGIONS = [
    'ap-bangkok', 'ap-beijing', 'ap-chengdu', 'ap-chongqing',
    'ap-guangzhou', 'ap-hongkong', 'ap-jakarta',
    'ap-nanjing', 'ap-seoul', 'ap-shanghai', 'ap-shanghai-fsi',
    'ap-shenzhen-fsi', 'ap-singapore', 'ap-tokyo', 'eu-frankfurt',
    'na-ashburn', 'na-siliconvalley', 'sa-saopaulo',
]


def _resolve_credentials():
    """Resolve Tencent Cloud credentials from environment variables."""
    candidates = [
        ('TENCENTCLOUD_SECRETID', 'TENCENTCLOUD_SECRETKEY', 'TENCENTCLOUD_SESSIONTOKEN'),
        ('TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY', 'TENCENTCLOUD_SESSION_TOKEN'),
    ]
    for id_key, key_key, token_key in candidates:
        sid = os.environ.get(id_key)
        skey = os.environ.get(key_key)
        if sid and skey:
            token = os.environ.get(token_key)
            logger.info(f"Using credentials from env vars: {id_key}, token={'yes' if token else 'no'}")
            return sid, skey, token
    raise RuntimeError(
        "No credentials found. Configure SCF execution role or set "
        "TENCENTCLOUD_SECRETID/TENCENTCLOUD_SECRETKEY env vars."
    )


def run():
    logger.info("=" * 60)
    logger.info(f"Tencent Cloud Resource Cleaner v{__version__}")
    logger.info(f"Mode: {'DRY RUN' if DRY_RUN else 'PRODUCTION'}")
    logger.info(f"Services: CLB={ENABLE_CLB}  CBS={ENABLE_CBS}  EIP={ENABLE_EIP}  ENI={ENABLE_ENI}  HAVIP={ENABLE_HAVIP}  SNAP={ENABLE_SNAPSHOT}  NAT={ENABLE_NAT}  AS={ENABLE_AS}")
    logger.info(f"Default TTL: {DEFAULT_TTL_DAYS} days")
    logger.info("=" * 60)

    secret_id, secret_key, token = _resolve_credentials()
    regions = REGIONS if REGIONS and REGIONS[0] else ALL_REGIONS
    logger.info(f"Processing {len(regions)} regions: {', '.join(regions)}")

    # Build service cleaners
    service_config = {
        'clb':      (ENABLE_CLB,      CLBCleaner),
        'cbs':      (ENABLE_CBS,      CBSCleaner),
        'eip':      (ENABLE_EIP,      EIPCleaner),
        'eni':      (ENABLE_ENI,      ENICleaner),
        'havip':    (ENABLE_HAVIP,    HAVIPCleaner),
        'snapshot': (ENABLE_SNAPSHOT, SnapshotCleaner),
        'nat':      (ENABLE_NAT,      NATCleaner),
        'as':       (ENABLE_AS,       ASCleaner),
    }

    cleaners = {}
    for name, (enabled, cls) in service_config.items():
        if enabled:
            cleaners[name] = cls(secret_id, secret_key, token, DRY_RUN, DEFAULT_TTL_DAYS)

    # Process regions
    for region in regions:
        for name, cleaner in cleaners.items():
            try:
                cleaner.process_region(region)
            except Exception as e:
                logger.error(f"Failed to process {name.upper()} in {region}: {e}")

    # Summary
    stats = {}
    logger.info("=" * 60)
    logger.info("Execution Summary:")
    for name, cleaner in cleaners.items():
        s = cleaner.stats
        stats[name] = s
        label = name.upper()
        action = 'released' if name == 'eip' else 'deleted'
        logger.info(f"{label}:")
        logger.info(f"  Total scanned: {s['total_scanned']}")
        logger.info(f"  Pending deletion: {s['pending_deletion']}")
        logger.info(f"  Successfully {action}: {s['deleted']}")
        logger.info(f"  Skipped: {s['skipped']}")
        logger.info(f"  Errors: {s['errors']}")
    logger.info("=" * 60)

    return stats


def main_handler(event, context):
    try:
        stats = run()
        return {
            'statusCode': 200,
            'body': json.dumps({'version': __version__, **stats}),
        }
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)}),
        }


if __name__ == '__main__':
    run()

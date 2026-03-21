"""
Base service cleaner with shared utilities.

All service cleaners inherit from this class to reuse:
- Tencent Cloud SDK client creation
- Tag parsing helpers
- TTL expiration logic
- Stats tracking
"""

import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

logger = logging.getLogger(__name__)

TAG_CAN_DELETE = 'TaggerCanDelete'
TAG_TTL = 'TaggerTTL'
TAG_CREATED = 'TaggerCreated'
TAG_PROJECT = 'TaggerProject'
TAG_LINKED_CVM = 'TaggerLinkedCVM'
TAG_LINKED_RESOURCE = 'TaggerLinkedResource'


class BaseCleaner:
    """Base class for all service cleaners."""

    service_name: str = 'base'

    def __init__(self, secret_id: str, secret_key: str, token: Optional[str],
                 dry_run: bool, default_ttl_days: int):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.token = token
        self.dry_run = dry_run
        self.default_ttl_days = default_ttl_days
        self.stats = {
            'total_scanned': 0,
            'pending_deletion': 0,
            'deleted': 0,
            'skipped': 0,
            'errors': 0,
        }

    def _make_client(self, client_cls, endpoint: str, region: str):
        """Create a Tencent Cloud SDK client."""
        if self.token:
            cred = credential.Credential(self.secret_id, self.secret_key, self.token)
        else:
            cred = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = endpoint
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        return client_cls(cred, region, client_profile)

    # ── Tag helpers ──────────────────────────────────────────────

    @staticmethod
    def get_tag_value(tags: List, tag_key: str) -> Optional[str]:
        """Extract tag value using TagKey/TagValue attributes."""
        if not tags:
            return None
        for tag in tags:
            if hasattr(tag, 'TagKey') and tag.TagKey == tag_key:
                return tag.TagValue
        return None

    @staticmethod
    def get_tag_value_kv(tags: List, tag_key: str) -> Optional[str]:
        """Extract tag value using Key/Value attributes (EIP / VPC resources)."""
        if not tags:
            return None
        for tag in tags:
            key = getattr(tag, 'Key', None) or getattr(tag, 'TagKey', None)
            value = getattr(tag, 'Value', None) or getattr(tag, 'TagValue', None)
            if key == tag_key:
                return value
        return None

    @staticmethod
    def parse_date(date_str: str) -> Optional[datetime]:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to parse date '{date_str}': {str(e)}")
            return None

    # ── Common TTL / deletion decision ───────────────────────────

    def check_ttl_expired(self, resource_id: str, tag_ttl: str,
                          tag_created: str) -> Tuple[bool, int, int, Optional[str]]:
        """
        Returns (expired: bool, age_days, ttl_days, error_reason_or_None).
        """
        if not tag_ttl or not tag_created:
            return False, 0, 0, "Missing required tags (TaggerTTL or TaggerCreated)"

        try:
            ttl_days = int(tag_ttl)
        except ValueError:
            logger.warning(f"Invalid TTL value '{tag_ttl}' for {resource_id}, using default {self.default_ttl_days}")
            ttl_days = self.default_ttl_days

        created_date = self.parse_date(tag_created)
        if not created_date:
            return False, 0, ttl_days, f"Invalid TaggerCreated date format: {tag_created}"

        age_days = (datetime.now() - created_date).days
        if age_days < ttl_days:
            return False, age_days, ttl_days, f"Not expired yet (age: {age_days} days, TTL: {ttl_days} days)"

        return True, age_days, ttl_days, None

    def standard_delete_decision(self, tag_can_delete: Optional[str],
                                 tag_project: Optional[str],
                                 age_days: int, ttl_days: int) -> Tuple[bool, str]:
        """
        Standard deletion logic used by CLB, EIP, and similar services.

        Delete if TTL expired AND:
          1. TaggerCanDelete=YES
          2. TaggerCanDelete=NO + TaggerProject=n/a
          3. No TaggerCanDelete + TaggerProject is n/a or missing
        """
        if tag_can_delete and tag_can_delete.upper() == 'YES':
            return True, f"TTL expired ({age_days}/{ttl_days} days) and TaggerCanDelete=YES"

        if tag_can_delete and tag_can_delete.upper() == 'NO':
            if tag_project and tag_project.lower() != 'n/a':
                return False, f"TTL expired but TaggerCanDelete=NO and TaggerProject={tag_project}"
            else:
                return True, f"TTL expired ({age_days}/{ttl_days} days), TaggerCanDelete=NO but TaggerProject=n/a"

        if tag_project and tag_project.lower() != 'n/a':
            return False, f"TTL expired but TaggerProject={tag_project} (no explicit delete tag)"

        return True, f"TTL expired ({age_days}/{ttl_days} days) and no protection (TaggerProject=n/a or missing)"

    # ── Interface ────────────────────────────────────────────────

    def process_region(self, region: str):
        raise NotImplementedError

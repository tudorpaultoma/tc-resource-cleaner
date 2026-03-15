"""
HAVIP (High Availability Virtual IP) Cleaner

Deletion strategy:
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: vpc.tencentcloudapi.com
  - DescribeHaVips
  - DeleteHaVip

CAM namespace: name/cvm:DescribeHaVips, name/cvm:DeleteHaVip

Note: HAVIP tags use Key/Value and are in TagSet (same as EIP/ENI).
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_CAN_DELETE, TAG_PROJECT,
)

logger = logging.getLogger(__name__)


class HAVIPCleaner(BaseCleaner):
    service_name = 'havip'

    def _get_client(self, region: str):
        return self._make_client(vpc_client.VpcClient, "vpc.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, havip_info: Dict) -> Tuple[bool, str]:
        havip_id = havip_info.get('HaVipId', 'unknown')
        tags = havip_info.get('Tags', [])

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(havip_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_havips, offset, limit = [], 0, 100

            while True:
                request = vpc_models.DescribeHaVipsRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeHaVips(request)
                havips = response.HaVipSet if hasattr(response, 'HaVipSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_havips.extend(havips)
                if len(all_havips) >= total or not havips:
                    break
                offset += limit

            tagged = []
            for havip in all_havips:
                tag_set = getattr(havip, 'TagSet', []) or []
                for tag in tag_set:
                    key = getattr(tag, 'Key', None) or getattr(tag, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(havip)
                        break

            logger.info(f"Found {len(all_havips)} total HAVIPs, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe HAVIPs in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, havip_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete HAVIP {havip_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DeleteHaVipRequest()
            request.from_json_string(json.dumps({"HaVipId": havip_id}))
            resp = client.DeleteHaVip(request)
            logger.info(f"Deleted HAVIP {havip_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete HAVIP {havip_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting HAVIP {havip_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing HAVIP in region: {region}")
        havips = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(havips)

        for havip in havips:
            havip_id = havip.HaVipId
            havip_name = getattr(havip, 'HaVipName', 'N/A')
            vip = getattr(havip, 'Vip', 'N/A')

            havip_dict = {
                'HaVipId': havip_id,
                'HaVipName': havip_name,
                'Tags': getattr(havip, 'TagSet', []),
            }

            should, reason = self.should_delete(havip_dict)
            if should:
                logger.info(f"HAVIP {havip_id} ({havip_name}, {vip}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, havip_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"HAVIP {havip_id} ({havip_name}, {vip}) skipped: {reason}")
                self.stats['skipped'] += 1

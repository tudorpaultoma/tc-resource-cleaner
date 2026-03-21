"""
CBS (Cloud Block Storage) Cleaner

Deletion strategy (stricter than CLB/EIP):
  Delete ONLY if TTL expired AND:
    - TaggerLinkedCVM=NO + TaggerProject=n/a
    - No TaggerLinkedCVM + TaggerProject=n/a

  Never delete if:
    - TaggerLinkedCVM=YES
    - TaggerProject has a real value

API: cbs.tencentcloudapi.com
CAM namespace: name/cvm:DescribeDisks, name/cvm:TerminateDisks

Note: CBS disk tags use Key/Value attributes (not TagKey/TagValue).
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_PROJECT, TAG_LINKED_CVM,
)

logger = logging.getLogger(__name__)


class CBSCleaner(BaseCleaner):
    service_name = 'cbs'

    def _get_client(self, region: str):
        return self._make_client(cbs_client.CbsClient, "cbs.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, disk_info: Dict) -> Tuple[bool, str]:
        disk_id = disk_info.get('DiskId', 'unknown')
        tags = disk_info.get('Tags', [])

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)
        tag_linked_cvm = self.get_tag_value_kv(tags, TAG_LINKED_CVM)

        expired, age, ttl, reason = self.check_ttl_expired(disk_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        if tag_linked_cvm and tag_linked_cvm.upper() == 'YES':
            return False, "TTL expired but TaggerLinkedCVM=YES (protected - pending strategy implementation)"

        if tag_linked_cvm and tag_linked_cvm.upper() == 'NO':
            if tag_project and tag_project.lower() == 'n/a':
                return True, f"TTL expired ({age}/{ttl} days), TaggerLinkedCVM=NO and TaggerProject=n/a"
            elif not tag_project or tag_project == '':
                return True, f"TTL expired ({age}/{ttl} days), TaggerLinkedCVM=NO and TaggerProject empty"
            else:
                return False, f"TTL expired but TaggerLinkedCVM=NO and TaggerProject={tag_project} (protected)"

        if tag_project and tag_project.lower() == 'n/a':
            return True, f"TTL expired ({age}/{ttl} days), no TaggerLinkedCVM and TaggerProject=n/a"

        return False, f"TTL expired but protected by TaggerProject={tag_project}"

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_disks, offset, limit = [], 0, 100

            while True:
                request = cbs_models.DescribeDisksRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeDisks(request)
                disks = response.DiskSet if hasattr(response, 'DiskSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_disks.extend(disks)
                if len(all_disks) >= total or not disks:
                    break
                offset += limit

            tagged = []
            for d in all_disks:
                tag_list = getattr(d, 'Tags', []) or []
                for t in tag_list:
                    key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(d)
                        break
            logger.info(f"Found {len(all_disks)} total CBS disks, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe CBS disks in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, disk_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete CBS disk {disk_id} in {region}")
                return True
            client = self._get_client(region)
            request = cbs_models.TerminateDisksRequest()
            request.from_json_string(json.dumps({"DiskIds": [disk_id]}))
            resp = client.TerminateDisks(request)
            logger.info(f"Deleted CBS disk {disk_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete CBS disk {disk_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting CBS disk {disk_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing CBS in region: {region}")
        disks = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(disks)

        for disk in disks:
            disk_id = disk.DiskId
            disk_name = getattr(disk, 'DiskName', 'N/A')
            disk_dict = {
                'DiskId': disk_id,
                'DiskName': disk_name,
                'Tags': getattr(disk, 'Tags', []),
            }

            should, reason = self.should_delete(disk_dict)
            if should:
                logger.info(f"CBS {disk_id} ({disk_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, disk_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"CBS {disk_id} ({disk_name}) skipped: {reason}")
                self.stats['skipped'] += 1

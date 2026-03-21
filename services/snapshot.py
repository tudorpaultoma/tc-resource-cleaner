"""
CBS Snapshot Cleaner

Deletion strategy:
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

  Only snapshots in NORMAL state can be deleted.

API: cbs.tencentcloudapi.com
  - DescribeSnapshots
  - DeleteSnapshots

CAM namespace: name/cvm:DescribeSnapshots, name/cvm:DeleteSnapshots

Note: Snapshot tags use Key/Value attributes.
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_CAN_DELETE, TAG_PROJECT,
)

logger = logging.getLogger(__name__)


class SnapshotCleaner(BaseCleaner):
    service_name = 'snapshot'

    def _get_client(self, region: str):
        return self._make_client(cbs_client.CbsClient, "cbs.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, snap_info: Dict) -> Tuple[bool, str]:
        snap_id = snap_info.get('SnapshotId', 'unknown')
        state = snap_info.get('SnapshotState', '')
        tags = snap_info.get('Tags', [])

        # Only NORMAL snapshots can be deleted
        if state and state.upper() != 'NORMAL':
            return False, f"State is {state}, only NORMAL snapshots can be deleted"

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(snap_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_snaps, offset, limit = [], 0, 100

            while True:
                request = cbs_models.DescribeSnapshotsRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeSnapshots(request)
                snaps = response.SnapshotSet if hasattr(response, 'SnapshotSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_snaps.extend(snaps)
                if len(all_snaps) >= total or not snaps:
                    break
                offset += limit

            tagged = []
            for s in all_snaps:
                tag_list = getattr(s, 'Tags', []) or []
                for t in tag_list:
                    key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(s)
                        break

            logger.info(f"Found {len(all_snaps)} total snapshots, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe snapshots in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, snap_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete snapshot {snap_id} in {region}")
                return True
            client = self._get_client(region)
            request = cbs_models.DeleteSnapshotsRequest()
            request.from_json_string(json.dumps({"SnapshotIds": [snap_id]}))
            resp = client.DeleteSnapshots(request)
            logger.info(f"Deleted snapshot {snap_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete snapshot {snap_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting snapshot {snap_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing Snapshots in region: {region}")
        snaps = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(snaps)

        for snap in snaps:
            snap_id = snap.SnapshotId
            snap_name = getattr(snap, 'SnapshotName', 'N/A')
            snap_state = getattr(snap, 'SnapshotState', 'UNKNOWN')
            disk_id = getattr(snap, 'DiskId', 'N/A')

            snap_dict = {
                'SnapshotId': snap_id,
                'SnapshotName': snap_name,
                'SnapshotState': snap_state,
                'DiskId': disk_id,
                'Tags': getattr(snap, 'Tags', []),
            }

            should, reason = self.should_delete(snap_dict)
            if should:
                logger.info(f"Snapshot {snap_id} ({snap_name}, disk={disk_id}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, snap_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"Snapshot {snap_id} ({snap_name}, disk={disk_id}) skipped: {reason}")
                self.stats['skipped'] += 1

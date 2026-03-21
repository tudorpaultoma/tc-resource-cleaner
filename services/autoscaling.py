"""
Auto Scaling Cleaner (Scaling Groups + Launch Configurations)

Deletion strategy:
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

  Scaling groups: only deleted if InstanceCount == 0 (no running instances).
  Launch configs: only deleted if not referenced by any scaling group.

API: as.tencentcloudapi.com
  - DescribeAutoScalingGroups / DeleteAutoScalingGroup
  - DescribeLaunchConfigurations / DeleteLaunchConfiguration

CAM namespace: name/as:DescribeAutoScalingGroups, name/as:DeleteAutoScalingGroup,
               name/as:DescribeLaunchConfigurations, name/as:DeleteLaunchConfiguration

Note: AS tags use Key/Value attributes in Tags.
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.autoscaling.v20180419 import autoscaling_client, models as as_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_CAN_DELETE, TAG_PROJECT,
)

logger = logging.getLogger(__name__)


class ASCleaner(BaseCleaner):
    service_name = 'as'

    def _get_client(self, region: str):
        return self._make_client(
            autoscaling_client.AutoscalingClient,
            "as.tencentcloudapi.com", region,
        )

    # ── Decision (Scaling Group) ─────────────────────────────────

    def should_delete_asg(self, asg_info: Dict) -> Tuple[bool, str]:
        asg_id = asg_info.get('AutoScalingGroupId', 'unknown')
        instance_count = asg_info.get('InstanceCount', -1)
        tags = asg_info.get('Tags', [])

        # Never delete groups with running instances
        if instance_count > 0:
            return False, f"Has {instance_count} running instance(s), cannot delete"

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(asg_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Decision (Launch Configuration) ──────────────────────────

    def should_delete_lc(self, lc_info: Dict, active_lc_ids: set) -> Tuple[bool, str]:
        lc_id = lc_info.get('LaunchConfigurationId', 'unknown')
        tags = lc_info.get('Tags', [])

        # Never delete launch configs referenced by a scaling group
        if lc_id in active_lc_ids:
            return False, "Referenced by an active scaling group"

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(lc_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe (Scaling Groups) ────────────────────────────────

    def describe_asgs(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_asgs, offset, limit = [], 0, 100

            while True:
                request = as_models.DescribeAutoScalingGroupsRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeAutoScalingGroups(request)
                asgs = response.AutoScalingGroupSet if hasattr(response, 'AutoScalingGroupSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_asgs.extend(asgs)
                if len(all_asgs) >= total or not asgs:
                    break
                offset += limit
            return all_asgs

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe scaling groups in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Describe (Launch Configurations) ─────────────────────────

    def describe_lcs(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_lcs, offset, limit = [], 0, 100

            while True:
                request = as_models.DescribeLaunchConfigurationsRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeLaunchConfigurations(request)
                lcs = response.LaunchConfigurationSet if hasattr(response, 'LaunchConfigurationSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_lcs.extend(lcs)
                if len(all_lcs) >= total or not lcs:
                    break
                offset += limit
            return all_lcs

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe launch configs in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete (Scaling Group) ───────────────────────────────────

    def delete_asg(self, region: str, asg_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete scaling group {asg_id} in {region}")
                return True
            client = self._get_client(region)
            request = as_models.DeleteAutoScalingGroupRequest()
            request.from_json_string(json.dumps({"AutoScalingGroupId": asg_id}))
            resp = client.DeleteAutoScalingGroup(request)
            logger.info(f"Deleted scaling group {asg_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete scaling group {asg_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting scaling group {asg_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Delete (Launch Configuration) ────────────────────────────

    def delete_lc(self, region: str, lc_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete launch config {lc_id} in {region}")
                return True
            client = self._get_client(region)
            request = as_models.DeleteLaunchConfigurationRequest()
            request.from_json_string(json.dumps({"LaunchConfigurationId": lc_id}))
            resp = client.DeleteLaunchConfiguration(request)
            logger.info(f"Deleted launch config {lc_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete launch config {lc_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting launch config {lc_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing Auto Scaling in region: {region}")

        # --- Scaling Groups ---
        all_asgs = self.describe_asgs(region)
        tagged_asgs = []
        active_lc_ids = set()

        for asg in all_asgs:
            lc_id = getattr(asg, 'LaunchConfigurationId', None)
            if lc_id:
                active_lc_ids.add(lc_id)
            tag_list = getattr(asg, 'Tags', []) or []
            for t in tag_list:
                key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                if key == TAG_TTL:
                    tagged_asgs.append(asg)
                    break

        logger.info(f"Found {len(all_asgs)} total scaling groups, {len(tagged_asgs)} with {TAG_TTL} tag in {region}")
        self.stats['total_scanned'] += len(tagged_asgs)

        for asg in tagged_asgs:
            asg_id = asg.AutoScalingGroupId
            asg_name = getattr(asg, 'AutoScalingGroupName', 'N/A')
            instance_count = getattr(asg, 'InstanceCount', -1)

            asg_dict = {
                'AutoScalingGroupId': asg_id,
                'AutoScalingGroupName': asg_name,
                'InstanceCount': instance_count,
                'Tags': getattr(asg, 'Tags', []),
            }

            should, reason = self.should_delete_asg(asg_dict)
            if should:
                logger.info(f"ASG {asg_id} ({asg_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete_asg(region, asg_id):
                    self.stats['deleted'] += 1
                    # Remove from active set so LC can be cleaned too
                    lc_id = getattr(asg, 'LaunchConfigurationId', None)
                    if lc_id:
                        active_lc_ids.discard(lc_id)
            else:
                logger.info(f"ASG {asg_id} ({asg_name}) skipped: {reason}")
                self.stats['skipped'] += 1

        # --- Launch Configurations ---
        all_lcs = self.describe_lcs(region)
        tagged_lcs = []

        for lc in all_lcs:
            tag_list = getattr(lc, 'Tags', []) or []
            for t in tag_list:
                key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                if key == TAG_TTL:
                    tagged_lcs.append(lc)
                    break

        logger.info(f"Found {len(all_lcs)} total launch configs, {len(tagged_lcs)} with {TAG_TTL} tag in {region}")
        self.stats['total_scanned'] += len(tagged_lcs)

        for lc in tagged_lcs:
            lc_id = lc.LaunchConfigurationId
            lc_name = getattr(lc, 'LaunchConfigurationName', 'N/A')

            lc_dict = {
                'LaunchConfigurationId': lc_id,
                'LaunchConfigurationName': lc_name,
                'Tags': getattr(lc, 'Tags', []),
            }

            should, reason = self.should_delete_lc(lc_dict, active_lc_ids)
            if should:
                logger.info(f"LC {lc_id} ({lc_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete_lc(region, lc_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"LC {lc_id} ({lc_name}) skipped: {reason}")
                self.stats['skipped'] += 1

"""
ENI (Elastic Network Interface) Cleaner

Deletion strategy:
  Skip immediately if attached to a CVM (Attachment.InstanceId is set).
  Only detached (available) ENIs are candidates.

  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: vpc.tencentcloudapi.com
  - DescribeNetworkInterfaces
  - DeleteNetworkInterface

CAM namespace: name/cvm:DescribeNetworkInterfaces, name/cvm:DeleteNetworkInterface

Note: ENI tags use Key/Value and are in TagSet (same as EIP).
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_CAN_DELETE, TAG_PROJECT,
    TAG_LINKED_RESOURCE,
)

logger = logging.getLogger(__name__)


class ENICleaner(BaseCleaner):
    service_name = 'eni'

    def _get_client(self, region: str):
        return self._make_client(vpc_client.VpcClient, "vpc.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, eni_info: Dict) -> Tuple[bool, str]:
        eni_id = eni_info.get('NetworkInterfaceId', 'unknown')
        state = eni_info.get('State', '')
        instance_id = eni_info.get('InstanceId', '')
        is_primary = eni_info.get('Primary', False)
        tags = eni_info.get('Tags', [])

        # Never delete primary ENI (it's deleted with the CVM)
        if is_primary:
            return False, "Primary ENI (deleted with CVM)"

        # If attached to a CVM, skip — it will be cleaned with the CVM
        if instance_id:
            return False, f"Attached to instance {instance_id}, will be cleaned with CVM"

        # Only available (detached) ENIs can be deleted
        if state and state.upper() != 'AVAILABLE':
            return False, f"State is {state}, only AVAILABLE ENIs can be deleted"

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)
        tag_linked_resource = self.get_tag_value_kv(tags, TAG_LINKED_RESOURCE)

        if tag_linked_resource and tag_linked_resource.upper() != 'NONE':
            return False, f"TaggerLinkedResource={tag_linked_resource}, bound to instance"

        expired, age, ttl, reason = self.check_ttl_expired(eni_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_enis, offset, limit = [], 0, 100

            while True:
                request = vpc_models.DescribeNetworkInterfacesRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeNetworkInterfaces(request)
                enis = response.NetworkInterfaceSet if hasattr(response, 'NetworkInterfaceSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_enis.extend(enis)
                if len(all_enis) >= total or not enis:
                    break
                offset += limit

            tagged = []
            for eni in all_enis:
                tag_set = getattr(eni, 'TagSet', []) or []
                for tag in tag_set:
                    key = getattr(tag, 'Key', None) or getattr(tag, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(eni)
                        break

            logger.info(f"Found {len(all_enis)} total ENIs, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe ENIs in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, eni_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete ENI {eni_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DeleteNetworkInterfaceRequest()
            request.from_json_string(json.dumps({"NetworkInterfaceId": eni_id}))
            resp = client.DeleteNetworkInterface(request)
            logger.info(f"Deleted ENI {eni_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete ENI {eni_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting ENI {eni_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing ENI in region: {region}")
        enis = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(enis)

        for eni in enis:
            eni_id = eni.NetworkInterfaceId
            eni_name = getattr(eni, 'NetworkInterfaceName', 'N/A')
            state = getattr(eni, 'State', 'UNKNOWN')
            primary = getattr(eni, 'Primary', False)

            # Get attached instance ID from Attachment
            attachment = getattr(eni, 'Attachment', None)
            instance_id = getattr(attachment, 'InstanceId', '') if attachment else ''

            eni_dict = {
                'NetworkInterfaceId': eni_id,
                'NetworkInterfaceName': eni_name,
                'State': state,
                'Primary': primary,
                'InstanceId': instance_id,
                'Tags': getattr(eni, 'TagSet', []),
            }

            should, reason = self.should_delete(eni_dict)
            if should:
                logger.info(f"ENI {eni_id} ({eni_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, eni_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"ENI {eni_id} ({eni_name}) skipped: {reason}")
                self.stats['skipped'] += 1

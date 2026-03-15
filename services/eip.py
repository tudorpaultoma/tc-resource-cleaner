"""
EIP (Elastic IP) Cleaner

Deletion strategy:
  Skip immediately if bound (BIND/BIND_ENI or TaggerLinkedResource != NONE).
  Only UNBIND EIPs are candidates.

  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: vpc.tencentcloudapi.com
CAM namespace: name/cvm:DescribeAddresses, name/cvm:ReleaseAddresses

Note: EIP tags use Key/Value (not TagKey/TagValue) and are in TagSet.
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


class EIPCleaner(BaseCleaner):
    service_name = 'eip'

    def _get_client(self, region: str):
        return self._make_client(vpc_client.VpcClient, "vpc.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, eip_info: Dict) -> Tuple[bool, str]:
        address_id = eip_info.get('AddressId', 'unknown')
        address_status = eip_info.get('AddressStatus', '')
        instance_id = eip_info.get('InstanceId', '')
        tags = eip_info.get('Tags', [])

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)
        tag_linked_resource = self.get_tag_value_kv(tags, TAG_LINKED_RESOURCE)

        # Bound to instance → skip
        if address_status in ('BIND', 'BIND_ENI'):
            return False, f"Bound to instance ({instance_id}), will be cleaned with CVM"
        if tag_linked_resource and tag_linked_resource.upper() != 'NONE':
            return False, f"TaggerLinkedResource={tag_linked_resource}, bound to instance"
        if address_status != 'UNBIND':
            return False, f"Status is {address_status}, only UNBIND can be released"

        expired, age, ttl, reason = self.check_ttl_expired(address_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_eips, offset, limit = [], 0, 100

            while True:
                request = vpc_models.DescribeAddressesRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeAddresses(request)
                eips = response.AddressSet if hasattr(response, 'AddressSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_eips.extend(eips)
                if len(all_eips) >= total or not eips:
                    break
                offset += limit

            tagged = []
            for eip in all_eips:
                tag_set = getattr(eip, 'TagSet', []) or []
                for tag in tag_set:
                    key = getattr(tag, 'Key', None) or getattr(tag, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(eip)
                        break

            logger.info(f"Found {len(all_eips)} total EIPs, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe EIPs in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, address_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would release EIP {address_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.ReleaseAddressesRequest()
            request.from_json_string(json.dumps({"AddressIds": [address_id]}))
            resp = client.ReleaseAddresses(request)
            logger.info(f"Released EIP {address_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to release EIP {address_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error releasing EIP {address_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing EIP in region: {region}")
        eips = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(eips)

        for eip in eips:
            address_id = eip.AddressId
            address_name = getattr(eip, 'AddressName', 'N/A')
            address_ip = getattr(eip, 'AddressIp', 'N/A')
            address_status = getattr(eip, 'AddressStatus', 'UNKNOWN')
            instance_id = getattr(eip, 'InstanceId', '')

            eip_dict = {
                'AddressId': address_id,
                'AddressName': address_name,
                'AddressStatus': address_status,
                'InstanceId': instance_id,
                'Tags': getattr(eip, 'TagSet', []),
            }

            should, reason = self.should_delete(eip_dict)
            if should:
                logger.info(f"EIP {address_id} ({address_name}, {address_ip}) marked for release: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, address_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"EIP {address_id} ({address_name}, {address_ip}) skipped: {reason}")
                self.stats['skipped'] += 1

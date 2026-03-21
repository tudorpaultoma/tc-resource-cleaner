"""
NAT Gateway (Public) Cleaner

Deletion strategy:
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: vpc.tencentcloudapi.com
  - DescribeNatGateways
  - DeleteNatGateway

CAM namespace: name/vpc:DescribeNatGateways, name/vpc:DeleteNatGateway

Note: NAT Gateway tags use Key/Value attributes in TagSet.
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


class NATCleaner(BaseCleaner):
    service_name = 'nat'

    def _get_client(self, region: str):
        return self._make_client(vpc_client.VpcClient, "vpc.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, nat_info: Dict) -> Tuple[bool, str]:
        nat_id = nat_info.get('NatGatewayId', 'unknown')
        tags = nat_info.get('Tags', [])

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(nat_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_nats, offset, limit = [], 0, 100

            while True:
                request = vpc_models.DescribeNatGatewaysRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeNatGateways(request)
                nats = response.NatGatewaySet if hasattr(response, 'NatGatewaySet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_nats.extend(nats)
                if len(all_nats) >= total or not nats:
                    break
                offset += limit

            tagged = []
            for n in all_nats:
                tag_set = getattr(n, 'TagSet', []) or []
                for t in tag_set:
                    key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                    if key == TAG_TTL:
                        tagged.append(n)
                        break

            logger.info(f"Found {len(all_nats)} total NAT Gateways, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe NAT Gateways in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, nat_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete NAT Gateway {nat_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DeleteNatGatewayRequest()
            request.from_json_string(json.dumps({"NatGatewayId": nat_id}))
            resp = client.DeleteNatGateway(request)
            logger.info(f"Deleted NAT Gateway {nat_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing NAT Gateway in region: {region}")
        nats = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(nats)

        for nat in nats:
            nat_id = nat.NatGatewayId
            nat_name = getattr(nat, 'NatGatewayName', 'N/A')

            nat_dict = {
                'NatGatewayId': nat_id,
                'NatGatewayName': nat_name,
                'Tags': getattr(nat, 'TagSet', []),
            }

            should, reason = self.should_delete(nat_dict)
            if should:
                logger.info(f"NAT Gateway {nat_id} ({nat_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, nat_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"NAT Gateway {nat_id} ({nat_name}) skipped: {reason}")
                self.stats['skipped'] += 1

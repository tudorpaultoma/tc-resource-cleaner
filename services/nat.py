"""
NAT Gateway (Public + Private) Cleaner

Covers both gateway types:
  • Public  NAT  — DescribeNatGateways  / DeleteNatGateway  (nat-xxxx)
  • Private NAT  — DescribePrivateNatGateways / DeletePrivateNatGateway (intranat-xxxx)

Deletion strategy (identical for both types):
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: vpc.tencentcloudapi.com
CAM namespace:
  name/vpc:DescribeNatGateways,  name/vpc:DeleteNatGateway
  name/vpc:DescribePrivateNatGateways, name/vpc:DeletePrivateNatGateway

Note: Both types use Key/Value attributes in TagSet.
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

    # ── Describe (public) ────────────────────────────────────────

    def _describe_public(self, region: str) -> List:
        """Return tagged public NAT gateways (nat-xxxx)."""
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

            tagged = self._filter_tagged(all_nats)
            logger.info(f"Found {len(all_nats)} public NAT Gateways, "
                        f"{len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e) or 'UnsupportedRegion' in str(e):
                logger.warning(f"Region {region} public NAT error: {e}")
                return []
            logger.error(f"Failed to describe public NAT Gateways in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error (public NAT) in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Describe (private) ───────────────────────────────────────

    def _describe_private(self, region: str) -> List:
        """Return tagged private NAT gateways (intranat-xxxx)."""
        try:
            client = self._get_client(region)
            all_nats, offset, limit = [], 0, 100

            while True:
                request = vpc_models.DescribePrivateNatGatewaysRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribePrivateNatGateways(request)
                nats = response.PrivateNatGatewaySet if hasattr(response, 'PrivateNatGatewaySet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_nats.extend(nats)
                if len(all_nats) >= total or not nats:
                    break
                offset += limit

            tagged = self._filter_tagged(all_nats)
            logger.info(f"Found {len(all_nats)} private NAT Gateways, "
                        f"{len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            err = str(e)
            if 'InvalidParameter' in err or 'UnsupportedRegion' in err or 'UnsupportedOperation' in err:
                logger.warning(f"Region {region} private NAT not supported or error: {e}")
                return []
            logger.error(f"Failed to describe private NAT Gateways in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error (private NAT) in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Shared filter helper ─────────────────────────────────────

    def _filter_tagged(self, nats: List) -> List:
        tagged = []
        for n in nats:
            tag_set = getattr(n, 'TagSet', []) or []
            for t in tag_set:
                key = getattr(t, 'Key', None) or getattr(t, 'TagKey', None)
                if key == TAG_TTL:
                    tagged.append(n)
                    break
        return tagged

    # ── Delete (public) ──────────────────────────────────────────

    def _delete_public(self, region: str, nat_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete public NAT Gateway {nat_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DeleteNatGatewayRequest()
            request.from_json_string(json.dumps({"NatGatewayId": nat_id}))
            resp = client.DeleteNatGateway(request)
            logger.info(f"Deleted public NAT Gateway {nat_id} in {region} "
                        f"(RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete public NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting public NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Delete (private) ─────────────────────────────────────────

    def _delete_private(self, region: str, nat_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete private NAT Gateway {nat_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DeletePrivateNatGatewayRequest()
            request.from_json_string(json.dumps({"NatGatewayId": nat_id}))
            resp = client.DeletePrivateNatGateway(request)
            logger.info(f"Deleted private NAT Gateway {nat_id} in {region} "
                        f"(RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete private NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting private NAT Gateway {nat_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process (shared) ─────────────────────────────────────────

    def _process_list(self, region: str, nats: List, kind: str, delete_fn):
        """Evaluate and delete a list of NAT gateways."""
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
                logger.info(f"{kind} NAT Gateway {nat_id} ({nat_name}) "
                            f"marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if delete_fn(region, nat_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"{kind} NAT Gateway {nat_id} ({nat_name}) "
                            f"skipped: {reason}")
                self.stats['skipped'] += 1

    def process_region(self, region: str):
        logger.info(f"Processing NAT Gateway in region: {region}")

        # Public NAT gateways
        public_nats = self._describe_public(region)
        self._process_list(region, public_nats, "Public", self._delete_public)

        # Private NAT gateways
        private_nats = self._describe_private(region)
        self._process_list(region, private_nats, "Private", self._delete_private)

"""
CLB (Cloud Load Balancer) Cleaner

Deletion strategy:
  Delete if TTL expired AND:
    1. TaggerCanDelete=YES
    2. TaggerCanDelete=NO + TaggerProject=n/a
    3. No TaggerCanDelete + TaggerProject is n/a or missing

API: clb.tencentcloudapi.com
CAM namespace: name/clb:*
"""

import json
import logging
from typing import List, Dict, Tuple

from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.clb.v20180317 import clb_client, models as clb_models

from services.base import (
    BaseCleaner, TAG_TTL, TAG_CREATED, TAG_CAN_DELETE, TAG_PROJECT,
)

logger = logging.getLogger(__name__)


class CLBCleaner(BaseCleaner):
    service_name = 'clb'

    def _get_client(self, region: str):
        return self._make_client(clb_client.ClbClient, "clb.tencentcloudapi.com", region)

    # ── Decision ─────────────────────────────────────────────────

    def should_delete(self, clb_info: Dict) -> Tuple[bool, str]:
        lb_id = clb_info.get('LoadBalancerId', 'unknown')
        tags = clb_info.get('Tags', [])

        tag_ttl = self.get_tag_value(tags, TAG_TTL)
        tag_created = self.get_tag_value(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value(tags, TAG_PROJECT)

        expired, age, ttl, reason = self.check_ttl_expired(lb_id, tag_ttl, tag_created)
        if not expired:
            return False, reason

        return self.standard_delete_decision(tag_can_delete, tag_project, age, ttl)

    # ── Describe ─────────────────────────────────────────────────

    def describe_with_tags(self, region: str) -> List:
        try:
            client = self._get_client(region)
            all_clbs, offset, limit = [], 0, 100

            while True:
                request = clb_models.DescribeLoadBalancersRequest()
                request.from_json_string(json.dumps({"Offset": offset, "Limit": limit}))
                response = client.DescribeLoadBalancers(request)
                clbs = response.LoadBalancerSet if hasattr(response, 'LoadBalancerSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_clbs.extend(clbs)
                if len(all_clbs) >= total or not clbs:
                    break
                offset += limit

            tagged = [c for c in all_clbs
                      if any(getattr(t, 'TagKey', None) == TAG_TTL
                             for t in (getattr(c, 'Tags', []) or []))]
            logger.info(f"Found {len(all_clbs)} total CLBs, {len(tagged)} with {TAG_TTL} tag in {region}")
            return tagged

        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e):
                logger.warning(f"Region {region} returned error: {e}")
                return []
            logger.error(f"Failed to describe CLBs in {region}: {e}")
            self.stats['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in {region}: {e}")
            self.stats['errors'] += 1
            return []

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, lb_id: str) -> bool:
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would delete CLB {lb_id} in {region}")
                return True
            client = self._get_client(region)
            request = clb_models.DeleteLoadBalancerRequest()
            request.from_json_string(json.dumps({"LoadBalancerIds": [lb_id]}))
            resp = client.DeleteLoadBalancer(request)
            logger.info(f"Deleted CLB {lb_id} in {region} (RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete CLB {lb_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting CLB {lb_id} in {region}: {e}")
            self.stats['errors'] += 1
            return False

    # ── Process ──────────────────────────────────────────────────

    def process_region(self, region: str):
        logger.info(f"Processing CLB in region: {region}")
        clbs = self.describe_with_tags(region)
        self.stats['total_scanned'] += len(clbs)

        for clb in clbs:
            lb_id = clb.LoadBalancerId
            lb_name = getattr(clb, 'LoadBalancerName', 'N/A')
            clb_dict = {
                'LoadBalancerId': lb_id,
                'LoadBalancerName': lb_name,
                'Tags': getattr(clb, 'Tags', []),
            }

            should, reason = self.should_delete(clb_dict)
            if should:
                logger.info(f"CLB {lb_id} ({lb_name}) marked for deletion: {reason}")
                self.stats['pending_deletion'] += 1
                if self.delete(region, lb_id):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"CLB {lb_id} ({lb_name}) skipped: {reason}")
                self.stats['skipped'] += 1

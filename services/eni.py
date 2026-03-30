"""
ENI (Elastic Network Interface) Cleaner

Deletion strategy:
  1. Actively attached to a running CVM → skip unconditionally.
  2. Primary ENI whose linked resource (TaggerLinkedResource) still exists
     → skip (deleted with its parent resource).
  3. Orphaned primary ENI — linked resource (TKE cluster, CVM, etc.) no
     longer exists → apply a 1-day grace period, then respect project
     protection before deleting.
  4. Non-primary, detached (AVAILABLE) ENI → standard TTL/project/CanDelete
     logic.

API: vpc.tencentcloudapi.com
  - DescribeNetworkInterfaces
  - DetachNetworkInterface  (used before delete for orphaned primary ENIs)
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

# Grace period (days) before deleting an orphaned ENI whose parent
# resource (TKE cluster, CVM, etc.) no longer exists.
ORPHAN_GRACE_DAYS = 1


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

        tag_ttl = self.get_tag_value_kv(tags, TAG_TTL)
        tag_created = self.get_tag_value_kv(tags, TAG_CREATED)
        tag_can_delete = self.get_tag_value_kv(tags, TAG_CAN_DELETE)
        tag_project = self.get_tag_value_kv(tags, TAG_PROJECT)
        tag_linked_resource = self.get_tag_value_kv(tags, TAG_LINKED_RESOURCE)

        # Actively attached to a running instance → skip
        if instance_id:
            return False, f"Attached to instance {instance_id}, will be cleaned with CVM"

        # Primary ENI handling
        if is_primary:
            # If TaggerLinkedResource is missing or NONE, we can't verify
            # the parent — play it safe and skip.
            if not tag_linked_resource or tag_linked_resource.upper() == 'NONE':
                return False, "Primary ENI with no linked resource info — skipping"

            # Linked resource is set but the ENI is no longer attached to
            # any instance → the parent resource (TKE cluster, CVM) was
            # likely deleted.  Apply orphan grace period + project check.
            expired, age, _, reason = self.check_ttl_expired(
                eni_id, str(ORPHAN_GRACE_DAYS), tag_created)
            if not expired:
                return False, (f"Orphaned primary ENI "
                               f"(TaggerLinkedResource={tag_linked_resource}), "
                               f"grace period not expired yet ({reason})")
            # Grace period expired — honour project protection
            if tag_project and tag_project.lower() != 'n/a':
                return False, (f"Orphaned primary ENI & grace expired, but "
                               f"TaggerProject={tag_project} — protected")
            return True, (f"Orphaned primary ENI "
                          f"(TaggerLinkedResource={tag_linked_resource}), "
                          f"grace {ORPHAN_GRACE_DAYS}d expired (age {age}d), "
                          f"TaggerProject=n/a — deleting")

        # Non-primary, must be available/detached
        if state and state.upper() != 'AVAILABLE':
            return False, f"State is {state}, only AVAILABLE ENIs can be deleted"

        # Orphaned secondary ENI (linked resource set but detached)
        if tag_linked_resource and tag_linked_resource.upper() != 'NONE':
            expired, age, _, reason = self.check_ttl_expired(
                eni_id, str(ORPHAN_GRACE_DAYS), tag_created)
            if not expired:
                return False, (f"Orphaned (TaggerLinkedResource="
                               f"{tag_linked_resource}), "
                               f"grace period not expired yet ({reason})")
            if tag_project and tag_project.lower() != 'n/a':
                return False, (f"Orphaned & grace expired, but "
                               f"TaggerProject={tag_project} — protected")
            return True, (f"Orphaned (TaggerLinkedResource="
                          f"{tag_linked_resource}), "
                          f"grace {ORPHAN_GRACE_DAYS}d expired (age {age}d), "
                          f"TaggerProject=n/a — deleting")

        # Standard path: no linked resource
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

    # ── Detach ───────────────────────────────────────────────────

    def detach(self, region: str, eni_id: str, instance_id: str) -> bool:
        """Detach an ENI from its (possibly ghost) instance. Returns True on success."""
        try:
            if self.dry_run:
                logger.info(f"[DRY RUN] Would detach ENI {eni_id} from {instance_id} in {region}")
                return True
            client = self._get_client(region)
            request = vpc_models.DetachNetworkInterfaceRequest()
            request.from_json_string(json.dumps({
                "NetworkInterfaceId": eni_id,
                "InstanceId": instance_id,
            }))
            resp = client.DetachNetworkInterface(request)
            logger.info(f"Detached ENI {eni_id} from {instance_id} in {region} "
                        f"(RequestId: {resp.RequestId})")
            return True
        except TencentCloudSDKException as e:
            logger.warning(f"Failed to detach ENI {eni_id} from {instance_id} in {region}: {e}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error detaching ENI {eni_id} in {region}: {e}")
            return False

    # ── Delete ───────────────────────────────────────────────────

    def delete(self, region: str, eni_id: str, attachment_instance_id: str = '',
               is_primary: bool = False) -> bool:
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
            if 'ResourceInUse' in str(e) and attachment_instance_id:
                # Primary ENIs cannot be detached (API returns
                # UnsupportedOperation) — skip the futile detach attempt.
                if is_primary:
                    logger.warning(
                        f"ENI {eni_id} is a zombie primary ENI in {region} — "
                        f"cannot delete (ResourceInUse) and cannot detach "
                        f"(primary ENIs are non-detachable). "
                        f"Ghost instance: {attachment_instance_id}. "
                        f"Requires Tencent Cloud support ticket or console cleanup.")
                    self.stats['errors'] += 1
                    return False
                # Non-primary: attempt detach then retry
                logger.info(f"ENI {eni_id} ResourceInUse — attempting detach from "
                            f"{attachment_instance_id} first")
                if self.detach(region, eni_id, attachment_instance_id):
                    try:
                        request2 = vpc_models.DeleteNetworkInterfaceRequest()
                        request2.from_json_string(json.dumps({"NetworkInterfaceId": eni_id}))
                        resp2 = client.DeleteNetworkInterface(request2)
                        logger.info(f"Deleted ENI {eni_id} in {region} after detach "
                                    f"(RequestId: {resp2.RequestId})")
                        return True
                    except TencentCloudSDKException as e2:
                        logger.error(f"Failed to delete ENI {eni_id} in {region} "
                                     f"even after detach: {e2}")
                        self.stats['errors'] += 1
                        return False
                else:
                    logger.error(f"Cannot clean ENI {eni_id} in {region}: "
                                 f"ResourceInUse and detach failed")
                    self.stats['errors'] += 1
                    return False
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
            raw_instance_id = getattr(attachment, 'InstanceId', '') if attachment else ''
            instance_id = raw_instance_id

            # Liveness check: if the instance no longer exists, treat the
            # ENI as orphaned by clearing instance_id.
            if instance_id and not self.instance_exists(region, instance_id):
                logger.info(f"ENI {eni_id} ({eni_name}): attached instance "
                            f"{instance_id} no longer exists — treating as orphaned")
                instance_id = ''

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
                if self.delete(region, eni_id, raw_instance_id, is_primary=primary):
                    self.stats['deleted'] += 1
            else:
                logger.info(f"ENI {eni_id} ({eni_name}) skipped: {reason}")
                self.stats['skipped'] += 1

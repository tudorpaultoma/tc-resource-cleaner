#!/usr/bin/env python3
import os
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional
from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.clb.v20180317 import clb_client, models as clb_models
from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = int(os.environ.get('DEFAULT_TTL_DAYS', '7'))
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
REGIONS = os.environ.get('REGIONS', '').split(',') if os.environ.get('REGIONS') else []
ENABLE_CLB = os.environ.get('ENABLE_CLB', 'true').lower() == 'true'
ENABLE_CBS = os.environ.get('ENABLE_CBS', 'true').lower() == 'true'

TAG_CAN_DELETE = 'TaggerCanDelete'
TAG_TTL = 'TaggerTTL'
TAG_CREATED = 'TaggerCreated'
TAG_PROJECT = 'TaggerProject'
TAG_LINKED_CVM = 'TaggerLinkedCVM'
TAG_USAGE = 'TaggerUsage'


class ResourceCleaner:
    
    def __init__(self, secret_id: Optional[str] = None, secret_key: Optional[str] = None):
        candidates = [
            ('TENCENTCLOUD_SECRETID', 'TENCENTCLOUD_SECRETKEY', 'TENCENTCLOUD_SESSIONTOKEN'),
            ('TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY', 'TENCENTCLOUD_SESSION_TOKEN'),
        ]
        
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.token = None
        
        if not self.secret_id or not self.secret_key:
            for id_key, key_key, token_key in candidates:
                sid = os.environ.get(id_key)
                skey = os.environ.get(key_key)
                if sid and skey:
                    self.secret_id = sid
                    self.secret_key = skey
                    self.token = os.environ.get(token_key)
                    logger.info(f"Using credentials from env vars: {id_key}, token={'yes' if self.token else 'no'}")
                    break
        
        if not self.secret_id or not self.secret_key:
            raise RuntimeError("No credentials found. Configure SCF execution role or set TENCENTCLOUD_SECRETID/TENCENTCLOUD_SECRETKEY env vars.")
        
        self.stats = {
            'clb': {
                'total_scanned': 0,
                'pending_deletion': 0,
                'deleted': 0,
                'skipped': 0,
                'errors': 0
            },
            'cbs': {
                'total_scanned': 0,
                'pending_deletion': 0,
                'deleted': 0,
                'skipped': 0,
                'errors': 0
            }
        }
    
    def get_clb_client(self, region: str):
        if self.token:
            cred = credential.Credential(self.secret_id, self.secret_key, self.token)
        else:
            cred = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "clb.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        return clb_client.ClbClient(cred, region, client_profile)
    
    def get_cbs_client(self, region: str):
        if self.token:
            cred = credential.Credential(self.secret_id, self.secret_key, self.token)
        else:
            cred = credential.Credential(self.secret_id, self.secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "cbs.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        return cbs_client.CbsClient(cred, region, client_profile)
    
    def get_all_regions(self) -> List[str]:
        return [
            'ap-bangkok', 'ap-beijing', 'ap-chengdu', 'ap-chongqing',
            'ap-guangzhou', 'ap-hongkong', 'ap-jakarta',
            'ap-nanjing', 'ap-seoul', 'ap-shanghai', 'ap-shanghai-fsi',
            'ap-shenzhen-fsi', 'ap-singapore', 'ap-tokyo', 'eu-frankfurt',
            'na-ashburn', 'na-siliconvalley',
            'sa-saopaulo'
        ]
    
    def parse_date(self, date_str: str) -> Optional[datetime]:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except (ValueError, TypeError) as e:
            logger.error(f"Failed to parse date '{date_str}': {str(e)}")
            return None
    
    def get_tag_value(self, tags: List, tag_key: str) -> Optional[str]:
        if not tags:
            return None
        for tag in tags:
            if hasattr(tag, 'TagKey') and tag.TagKey == tag_key:
                return tag.TagValue
        return None
    
    def should_delete_clb(self, clb_info: Dict) -> tuple[bool, str]:
        lb_id = clb_info.get('LoadBalancerId', 'unknown')
        tags = clb_info.get('Tags', [])
        
        tag_can_delete = self.get_tag_value(tags, TAG_CAN_DELETE)
        tag_ttl = self.get_tag_value(tags, TAG_TTL)
        tag_created = self.get_tag_value(tags, TAG_CREATED)
        tag_project = self.get_tag_value(tags, TAG_PROJECT)
        
        if not tag_ttl or not tag_created:
            return False, f"Missing required tags (TaggerTTL or TaggerCreated)"
        
        try:
            ttl_days = int(tag_ttl)
        except ValueError:
            logger.warning(f"Invalid TTL value '{tag_ttl}' for CLB {lb_id}, using default {DEFAULT_TTL_DAYS}")
            ttl_days = DEFAULT_TTL_DAYS
        
        created_date = self.parse_date(tag_created)
        if not created_date:
            return False, f"Invalid TaggerCreated date format: {tag_created}"
        
        current_date = datetime.now()
        age_days = (current_date - created_date).days
        
        if age_days < ttl_days:
            return False, f"Not expired yet (age: {age_days} days, TTL: {ttl_days} days)"
        
        if tag_can_delete and tag_can_delete.upper() == "YES":
            return True, f"TTL expired ({age_days}/{ttl_days} days) and TaggerCanDelete=YES"
        
        if tag_can_delete and tag_can_delete.upper() == "NO":
            if tag_project and tag_project.lower() != "n/a":
                return False, f"TTL expired but TaggerCanDelete=NO and TaggerProject={tag_project}"
            else:
                return True, f"TTL expired ({age_days}/{ttl_days} days), TaggerCanDelete=NO but TaggerProject=n/a"
        
        if tag_project and tag_project.lower() != "n/a":
            return False, f"TTL expired but TaggerProject={tag_project} (no explicit delete tag)"
        
        return True, f"TTL expired ({age_days}/{ttl_days} days) and no protection (TaggerProject=n/a or missing)"
    
    def describe_clbs_with_tags(self, region: str) -> List:
        try:
            client = self.get_clb_client(region)
            all_clbs = []
            offset = 0
            limit = 100
            
            while True:
                request = clb_models.DescribeLoadBalancersRequest()
                params = {"Offset": offset, "Limit": limit}
                request.from_json_string(json.dumps(params))
                
                response = client.DescribeLoadBalancers(request)
                clbs = response.LoadBalancerSet if hasattr(response, 'LoadBalancerSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_clbs.extend(clbs)
                
                if len(all_clbs) >= total or len(clbs) == 0:
                    break
                offset += limit
            
            # Filter: only CLBs that have a TaggerTTL tag
            tagged_clbs = []
            for clb in all_clbs:
                tags = getattr(clb, 'Tags', []) or []
                for tag in tags:
                    if hasattr(tag, 'TagKey') and tag.TagKey == TAG_TTL:
                        tagged_clbs.append(clb)
                        break
            
            logger.info(f"Found {len(all_clbs)} total CLBs, {len(tagged_clbs)} with {TAG_TTL} tag in region {region}")
            return tagged_clbs
            
        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e):
                logger.warning(f"Region {region} returned error: {str(e)}")
                return []
            logger.error(f"Failed to describe CLBs in region {region}: {str(e)}")
            self.stats['clb']['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in region {region}: {str(e)}")
            self.stats['clb']['errors'] += 1
            return []
    
    def delete_clb(self, region: str, lb_id: str) -> bool:
        try:
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would delete CLB {lb_id} in region {region}")
                return True
            
            client = self.get_clb_client(region)
            request = clb_models.DeleteLoadBalancerRequest()
            
            params = {"LoadBalancerIds": [lb_id]}
            request.from_json_string(json.dumps(params))
            
            response = client.DeleteLoadBalancer(request)
            request_id = response.RequestId
            
            logger.info(f"Successfully deleted CLB {lb_id} in region {region} (RequestId: {request_id})")
            return True
            
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete CLB {lb_id} in region {region}: {str(e)}")
            self.stats['clb']['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting CLB {lb_id} in region {region}: {str(e)}")
            self.stats['clb']['errors'] += 1
            return False
    
    def process_clb_region(self, region: str):
        logger.info(f"Processing CLB in region: {region}")
        
        clbs = self.describe_clbs_with_tags(region)
        self.stats['clb']['total_scanned'] += len(clbs)
        
        for clb in clbs:
            lb_id = clb.LoadBalancerId
            lb_name = getattr(clb, 'LoadBalancerName', 'N/A')
            
            clb_dict = {
                'LoadBalancerId': lb_id,
                'LoadBalancerName': lb_name,
                'Tags': getattr(clb, 'Tags', [])
            }
            
            should_delete, reason = self.should_delete_clb(clb_dict)
            
            if should_delete:
                logger.info(f"CLB {lb_id} ({lb_name}) marked for deletion: {reason}")
                self.stats['clb']['pending_deletion'] += 1
                
                if self.delete_clb(region, lb_id):
                    self.stats['clb']['deleted'] += 1
            else:
                logger.info(f"CLB {lb_id} ({lb_name}) skipped: {reason}")
                self.stats['clb']['skipped'] += 1
    
    def should_delete_cbs(self, disk_info: Dict) -> tuple[bool, str]:
        disk_id = disk_info.get('DiskId', 'unknown')
        tags = disk_info.get('Tags', [])
        
        tag_ttl = self.get_tag_value(tags, TAG_TTL)
        tag_created = self.get_tag_value(tags, TAG_CREATED)
        tag_project = self.get_tag_value(tags, TAG_PROJECT)
        tag_linked_cvm = self.get_tag_value(tags, TAG_LINKED_CVM)
        
        if not tag_ttl or not tag_created:
            return False, f"Missing required tags (TaggerTTL or TaggerCreated)"
        
        try:
            ttl_days = int(tag_ttl)
        except ValueError:
            logger.warning(f"Invalid TTL value '{tag_ttl}' for CBS {disk_id}, using default {DEFAULT_TTL_DAYS}")
            ttl_days = DEFAULT_TTL_DAYS
        
        created_date = self.parse_date(tag_created)
        if not created_date:
            return False, f"Invalid TaggerCreated date format: {tag_created}"
        
        current_date = datetime.now()
        age_days = (current_date - created_date).days
        
        if age_days < ttl_days:
            return False, f"Not expired yet (age: {age_days} days, TTL: {ttl_days} days)"
        
        if tag_linked_cvm and tag_linked_cvm.upper() == "YES":
            return False, f"TTL expired but TaggerLinkedCVM=YES (protected - pending strategy implementation)"
        
        if tag_linked_cvm and tag_linked_cvm.upper() == "NO":
            if tag_project and tag_project.lower() == "n/a":
                return True, f"TTL expired ({age_days}/{ttl_days} days), TaggerLinkedCVM=NO and TaggerProject=n/a"
            elif not tag_project or tag_project == "":
                return True, f"TTL expired ({age_days}/{ttl_days} days), TaggerLinkedCVM=NO and TaggerProject empty"
            else:
                return False, f"TTL expired but TaggerLinkedCVM=NO and TaggerProject={tag_project} (protected)"
        
        if tag_project and tag_project.lower() == "n/a":
            return True, f"TTL expired ({age_days}/{ttl_days} days), no TaggerLinkedCVM and TaggerProject=n/a"
        
        return False, f"TTL expired but protected by TaggerProject={tag_project}"
    
    def describe_cbs_with_tags(self, region: str) -> List:
        try:
            client = self.get_cbs_client(region)
            all_disks = []
            offset = 0
            limit = 100
            
            while True:
                request = cbs_models.DescribeDisksRequest()
                params = {"Offset": offset, "Limit": limit}
                request.from_json_string(json.dumps(params))
                
                response = client.DescribeDisks(request)
                disks = response.DiskSet if hasattr(response, 'DiskSet') else []
                total = response.TotalCount if hasattr(response, 'TotalCount') else 0
                all_disks.extend(disks)
                
                if len(all_disks) >= total or len(disks) == 0:
                    break
                offset += limit
            
            # Filter: only disks that have a TaggerTTL tag
            tagged_disks = []
            for disk in all_disks:
                tags = getattr(disk, 'Tags', []) or []
                for tag in tags:
                    if hasattr(tag, 'TagKey') and tag.TagKey == TAG_TTL:
                        tagged_disks.append(disk)
                        break
            
            logger.info(f"Found {len(all_disks)} total CBS disks, {len(tagged_disks)} with {TAG_TTL} tag in region {region}")
            return tagged_disks
            
        except TencentCloudSDKException as e:
            if 'InvalidParameter' in str(e):
                logger.warning(f"Region {region} returned error: {str(e)}")
                return []
            logger.error(f"Failed to describe CBS disks in region {region}: {str(e)}")
            self.stats['cbs']['errors'] += 1
            return []
        except Exception as e:
            logger.error(f"Unexpected error in region {region}: {str(e)}")
            self.stats['cbs']['errors'] += 1
            return []
    
    def delete_cbs(self, region: str, disk_id: str) -> bool:
        try:
            if DRY_RUN:
                logger.info(f"[DRY RUN] Would delete CBS disk {disk_id} in region {region}")
                return True
            
            client = self.get_cbs_client(region)
            request = cbs_models.TerminateDisksRequest()
            
            params = {"DiskIds": [disk_id]}
            request.from_json_string(json.dumps(params))
            
            response = client.TerminateDisks(request)
            request_id = response.RequestId
            
            logger.info(f"Successfully deleted CBS disk {disk_id} in region {region} (RequestId: {request_id})")
            return True
            
        except TencentCloudSDKException as e:
            logger.error(f"Failed to delete CBS disk {disk_id} in region {region}: {str(e)}")
            self.stats['cbs']['errors'] += 1
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting CBS disk {disk_id} in region {region}: {str(e)}")
            self.stats['cbs']['errors'] += 1
            return False
    
    def process_cbs_region(self, region: str):
        logger.info(f"Processing CBS in region: {region}")
        
        disks = self.describe_cbs_with_tags(region)
        self.stats['cbs']['total_scanned'] += len(disks)
        
        for disk in disks:
            disk_id = disk.DiskId
            disk_name = getattr(disk, 'DiskName', 'N/A')
            
            disk_dict = {
                'DiskId': disk_id,
                'DiskName': disk_name,
                'Tags': getattr(disk, 'Tags', [])
            }
            
            should_delete, reason = self.should_delete_cbs(disk_dict)
            
            if should_delete:
                logger.info(f"CBS {disk_id} ({disk_name}) marked for deletion: {reason}")
                self.stats['cbs']['pending_deletion'] += 1
                
                if self.delete_cbs(region, disk_id):
                    self.stats['cbs']['deleted'] += 1
            else:
                logger.info(f"CBS {disk_id} ({disk_name}) skipped: {reason}")
                self.stats['cbs']['skipped'] += 1
    
    def run(self):
        logger.info("=" * 60)
        logger.info("Tencent Cloud Resource Cleaner")
        logger.info(f"Mode: {'DRY RUN' if DRY_RUN else 'PRODUCTION'}")
        logger.info(f"Default TTL: {DEFAULT_TTL_DAYS} days")
        logger.info(f"CLB Enabled: {ENABLE_CLB}")
        logger.info(f"CBS Enabled: {ENABLE_CBS}")
        logger.info("=" * 60)
        
        regions = REGIONS if REGIONS and REGIONS[0] else self.get_all_regions()
        logger.info(f"Processing {len(regions)} regions: {', '.join(regions)}")
        
        for region in regions:
            try:
                if ENABLE_CLB:
                    self.process_clb_region(region)
                if ENABLE_CBS:
                    self.process_cbs_region(region)
            except Exception as e:
                logger.error(f"Failed to process region {region}: {str(e)}")
        
        logger.info("=" * 60)
        logger.info("Execution Summary:")
        logger.info("CLB:")
        logger.info(f"  Total scanned: {self.stats['clb']['total_scanned']}")
        logger.info(f"  Pending deletion: {self.stats['clb']['pending_deletion']}")
        logger.info(f"  Successfully deleted: {self.stats['clb']['deleted']}")
        logger.info(f"  Skipped: {self.stats['clb']['skipped']}")
        logger.info(f"  Errors: {self.stats['clb']['errors']}")
        logger.info("CBS:")
        logger.info(f"  Total scanned: {self.stats['cbs']['total_scanned']}")
        logger.info(f"  Pending deletion: {self.stats['cbs']['pending_deletion']}")
        logger.info(f"  Successfully deleted: {self.stats['cbs']['deleted']}")
        logger.info(f"  Skipped: {self.stats['cbs']['skipped']}")
        logger.info(f"  Errors: {self.stats['cbs']['errors']}")
        logger.info("=" * 60)
        
        return self.stats

def main_handler(event, context):
    try:
        cleaner = ResourceCleaner()
        stats = cleaner.run()
        
        return {
            'statusCode': 200,
            'body': json.dumps(stats)
        }
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


if __name__ == '__main__':
    cleaner = ResourceCleaner()
    cleaner.run()

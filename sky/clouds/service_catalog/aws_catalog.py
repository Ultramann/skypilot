"""AWS Offerings Catalog.

This module loads the service catalog file and can be used to query
instance types and pricing information for AWS.
"""
import colorama
import os
from typing import Dict, List, Optional, Tuple

import pandas as pd

import sky
from sky import resources
from sky import sky_logging
from sky.clouds import cloud
from sky.clouds.service_catalog import common
from sky.utils import ux_utils

logger = sky_logging.init_logger(__name__)

# Keep it synced with the frequency in
# skypilot-catalog/.github/workflows/update-aws-catalog.yml
_PULL_FREQUENCY_HOURS = 7

_df = common.read_catalog('aws/vms.csv',
                          pull_frequency_hours=_PULL_FREQUENCY_HOURS)
_image_df = common.read_catalog('aws/images.csv',
                                pull_frequency_hours=_PULL_FREQUENCY_HOURS)


def _apply_az_mapping(df: 'pd.DataFrame') -> 'pd.DataFrame':
    """Maps zone IDs (use1-az1) to zone names (us-east-1x).

    Such mappings are account-specific and determined by AWS.

    Returns:
        A dataframe with column 'AvailabilityZone' that's correctly replaced
        with the zone name (e.g. us-east-1a).
    """
    az_mapping_path = common.get_catalog_path('aws/az_mappings.csv')
    if not os.path.exists(az_mapping_path):
        # Fetch az mapping from AWS.
        # pylint: disable=import-outside-toplevel
        import ray
        from sky.clouds.service_catalog.data_fetchers import fetch_aws
        logger.info(f'{colorama.Style.DIM}Fetching availability zones mapping '
                    f'for AWS...{colorama.Style.RESET_ALL}')
        with ux_utils.suppress_output():
            ray.init()
        az_mappings = fetch_aws.fetch_availability_zone_mappings()
        az_mappings.to_csv(az_mapping_path, index=False)
    else:
        az_mappings = pd.read_csv(az_mapping_path)
    # Use inner join to drop rows with unknown AZ IDs, which are likely
    # because the user does not have access to that Region. Otherwise,
    # there will be rows with NaN in the AvailabilityZone column.
    df = df.merge(az_mappings, on=['AvailabilityZone'], how='inner')
    df = df.drop(columns=['AvailabilityZone']).rename(
        columns={'AvailabilityZoneName': 'AvailabilityZone'})
    return df


_df = _apply_az_mapping(_df)


def get_feasible_resources(
        resource_filter: resources.ResourceFilter
) -> List[resources.VMResources]:
    df = _df
    df = common.filter_spot(df, resource_filter.use_spot)

    if resource_filter.accelerator is None:
        acc_name = None
        acc_count = None
    else:
        acc_name = resource_filter.accelerator.name
        acc_count = resource_filter.accelerator.count
    filters = {
        'InstanceType': resource_filter.instance_type,
        'AcceleratorName': acc_name,
        'AcceleratorCount': acc_count,
        'Region': resource_filter.region,
        'AvailabilityZone': resource_filter.zone,
    }
    df = common.apply_filters(df, filters)
    df = df.reset_index(drop=True)

    feasible_resources = []
    aws = sky.AWS()
    for row in df.itertuples():
        if pd.isna(row.AcceleratorName) or pd.isna(row.AcceleratorCount):
            acc = None
        else:
            acc = resources.Accelerator(name=row.AcceleratorName,
                                        count=int(row.AcceleratorCount),
                                        args=None)
        feasible_resources.append(
            resources.VMResources(
                cloud=aws,
                region=row.Region,
                zone=row.AvailabilityZone,
                instance_type=row.InstanceType,
                num_vcpus=float(row.vCPUs),
                cpu_memory=float(row.MemoryGiB),
                accelerator=acc,
                use_spot=resource_filter.use_spot,
                spot_recovery=resource_filter.spot_recovery,
                disk_size=resource_filter.disk_size,
                image_id=resource_filter.image_id,
            ))
    return feasible_resources


def get_hourly_price(resource: resources.VMResources) -> float:
    return common.get_hourly_price_impl(_df, resource.instance_type,
                                        resource.zone, resource.use_spot)


def instance_type_exists(instance_type: str) -> bool:
    return common.instance_type_exists_impl(_df, instance_type)


def validate_region_zone(
        region: Optional[str],
        zone: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    return common.validate_region_zone_impl(_df, region, zone)


def accelerator_in_region_or_zone(acc_name: str,
                                  acc_count: int,
                                  region: Optional[str] = None,
                                  zone: Optional[str] = None) -> bool:
    return common.accelerator_in_region_or_zone_impl(_df, acc_name, acc_count,
                                                     region, zone)


def get_hourly_cost(instance_type: str,
                    use_spot: bool = False,
                    region: Optional[str] = None,
                    zone: Optional[str] = None) -> float:
    return common.get_hourly_cost_impl(_df, instance_type, use_spot, region,
                                       zone)


def get_vcpus_from_instance_type(instance_type: str) -> Optional[float]:
    return common.get_vcpus_from_instance_type_impl(_df, instance_type)


def get_accelerators_from_instance_type(
        instance_type: str) -> Optional[Dict[str, int]]:
    return common.get_accelerators_from_instance_type_impl(_df, instance_type)


def get_instance_type_for_accelerator(
    acc_name: str,
    acc_count: int,
    use_spot: bool = False,
    region: Optional[str] = None,
    zone: Optional[str] = None,
) -> Tuple[Optional[List[str]], List[str]]:
    """
    Returns a list of instance types satisfying the required count of
    accelerators with sorted prices and a list of candidates with fuzzy search.
    """
    return common.get_instance_type_for_accelerator_impl(df=_df,
                                                         acc_name=acc_name,
                                                         acc_count=acc_count,
                                                         use_spot=use_spot,
                                                         region=region,
                                                         zone=zone)


def get_region_zones_for_instance_type(instance_type: str,
                                       use_spot: bool) -> List['cloud.Region']:
    df = _df[_df['InstanceType'] == instance_type]
    region_list = common.get_region_zones(df, use_spot)
    # Hack: Enforce US regions are always tried first:
    #   [US regions sorted by price] + [non-US regions sorted by price]
    us_region_list = []
    other_region_list = []
    for region in region_list:
        if region.name.startswith('us-'):
            us_region_list.append(region)
        else:
            other_region_list.append(region)
    return us_region_list + other_region_list


def list_accelerators(gpus_only: bool,
                      name_filter: Optional[str],
                      case_sensitive: bool = True
                     ) -> Dict[str, List[common.InstanceTypeInfo]]:
    """Returns all instance types in AWS offering accelerators."""
    return common.list_accelerators_impl('AWS', _df, gpus_only, name_filter,
                                         case_sensitive)


def get_image_id_from_tag(tag: str, region: Optional[str]) -> Optional[str]:
    """Returns the image id from the tag."""
    return common.get_image_id_from_tag_impl(_image_df, tag, region)


def is_image_tag_valid(tag: str, region: Optional[str]) -> bool:
    """Returns whether the image tag is valid."""
    return common.is_image_tag_valid_impl(_image_df, tag, region)

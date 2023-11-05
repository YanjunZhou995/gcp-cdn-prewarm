import pulumi
import pulumi_gcp as gcp

config = pulumi.Config()
name = config.require('name')
type = config.require('type')

download_str = ''

with open('url.txt') as f:
    for line in f.readlines():
        line = line.strip()
        download_str += "curl -i %s > /dev/null;" % (line)


def create_vm(name, region, type):
    instance = gcp.compute.Instance(resource_name=name+'-'+region,
        machine_type=type,
        zone=region+"-b",
        boot_disk=gcp.compute.InstanceBootDiskArgs(
            initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                image="projects/ubuntu-os-cloud/global/images/ubuntu-2004-focal-v20231101",
                size = 30,
                type = "pd-balanced"
            ),
        ),
        network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(
            network="default",
            access_configs=[gcp.compute.InstanceNetworkInterfaceAccessConfigArgs()],
        )],
        metadata_startup_script='''
    #!/bin/bash
    for i in {1..3}; do
    '''+download_str+'''
    done''',
    )
    pulumi.export('instance_name', instance.name)

region_list = gcp.compute.get_regions()
for region in region_list.names:
    create_vm(name, region, type)
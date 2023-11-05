import pulumi
import pulumi_gcp as gcp

config = pulumi.Config();
name = config.require('name');
type = config.require('type')

url='https://static-gl.farlightgames.com/p/pcsdk/launcher/10060/prodef4621e968ad6b383b8754148b63/0/gamepack/498/gamepack.zip'

# region_list = ['us-west1','us-west2','us-west3','us-west4','us-central1','us-east1',
# 'us-east4','us-east5','us-south1' ,'northamerica-northeast1' ,'northamerica-northeast2' ,'southamerica-west1' ,
# 'southamerica-east1', 'europe-west2', 'europe-west1', 'europe-west4', 'europe-west6', 'europe-west3', 'europe-north1',
# 'europe-central2', 'europe-west8', 'europe-southwest1', 'europe-west9', 'europe-west12', 'europe-west10', 'asia-south1',
# 'asia-south2', 'asia-southeast1', 'asia-southeast2', 'asia-east2', 'asia-east1', 'asia-northeast1', 'asia-northeast2',
# 'australia-southeast1' ,'australia-southeast2' ,'asia-northeast3' ,'me-west1' ,'me-central1']

# #bucket = gcp.storage.Bucket(name, location="US")
# #pulumi.export('bucket_name', bucket.url)

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
    curl -i -r 0-100000000 '''+url+''' > /dev/null;
    done
    sudo shutdown -h now''',
    )
    pulumi.export('instance_name', instance.name)

region_list = gcp.compute.get_regions()
for region in region_list.names:
    create_vm(name, region, type)

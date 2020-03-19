#!/usr/bin/python3
import pynetbox
import atexit
import ipaddress
import urllib3
import logging 
import os
import datetime
import functools

from pyVim import connect
from pyVmomi import vmodl
from pyVmomi import vim
from pprint import pprint

vcenter_session = None
vcenter_content = None
netbox_client = None
logger = None

class VMwareCluster:
    def __init__(self, name, vcenter_persistent_id):
        self.name = name
        self.vcenter_persistent_id = vcenter_persistent_id

class NetboxCluster:
    def __init__(self, name, vcenter_persistent_id, raw_netbox_api_record):
        self.name = name
        self.vcenter_persistent_id = vcenter_persistent_id
        self.raw_netbox_api_record = raw_netbox_api_record
        
class VMwareVM:
    def __init__(self, name, uuid, vcpu, memory_mb, comment, power_state, vmtools_status, primary_ipaddress, is_template, custom_attributes, cluster_name):
        self.name = name
        self.uuid = uuid
        self.vcpu = vcpu
        self.memory_mb = memory_mb
        self.comment = comment
        self.power_state = power_state
        self.vmtools_status = vmtools_status
        self.primary_ipaddress = primary_ipaddress
        self.is_template = is_template
        self.custom_attributes = custom_attributes
        self.cluster_name = cluster_name

class NetboxVM:
    def __init__(self, name, vcenter_persistent_id, raw_netbox_api_record):
            self.name = name
            self.vcenter_persistent_id = vcenter_persistent_id
            self.raw_netbox_api_record = raw_netbox_api_record

def get_vcenter_clusters():
    vcenter_clusters = []
    
    # Get a list of datacenters in the vcenter
    vcenter_datacenters = vcenter_content.rootFolder.childEntity

    # We only have one datacenter, but lets loop through the list of them anyway
    for vcenter_datacenter in vcenter_datacenters:
        for vcenter_cluster in vcenter_datacenter.hostFolder.childEntity:
            vcenter_clusters.append( VMwareCluster( name = vcenter_cluster.name, 
                                                    vcenter_persistent_id = vcenter_cluster._moId) )

    return vcenter_clusters

def get_netbox_clusters():
    netbox_clusters = []

    try:
        nb_clusters = netbox_client.virtualization.clusters.all()
    except Exception as ex: 
        logger.error("Failed getting a list of netbox clusters")
        logger.exception(ex)
        raise SystemExit(-1)
    
    for nb_cluster in nb_clusters:
        if nb_cluster.type.name == "vSphere":
            netbox_clusters.append( NetboxCluster( name = nb_cluster.name,
                                                   vcenter_persistent_id = nb_cluster.custom_fields.get('vcenter_persistent_id'),
                                                   raw_netbox_api_record = nb_cluster) )
        
    return netbox_clusters

def update_netbox_clusters():

    vcenter_clusters = get_vcenter_clusters()
    netbox_clusters = get_netbox_clusters()

    # Find clusters present in netbox, but not in vsphere, and add comment
    # about it, on the netbox cluster object
    for nbc1 in netbox_clusters:
        if any(vc1.vcenter_persistent_id == nbc1.vcenter_persistent_id for vc1 in vcenter_clusters):
            logger.info(f"Cluster: {nbc1.name} with vCenter_ID: {nbc1.vcenter_persistent_id} exists in vcenter, nothing to do")
        else:
            logger.info(f"Cluster: {nbc1.name} with vCenter_ID: {nbc1.vcenter_persistent_id} does NOT exists in vcenter, adding comment to netbox")
            
            try:
                nbc1_update = netbox_client.virtualization.clusters.get(nbc1.raw_netbox_api_record.id)
                nbc1_update.comments = "No longer present in vCenter, verify manually, and delete this object in netbox"
                if nbc1_update.save():
                    logger.info("Successfully updated cluster object in netbox")
            except Exception as ex:
                logger.warn("Failed updating the cluster object in netbox")
                logger.exception(ex)

    # Find clusters present in vcenter, but not in netbox
    for vc2 in vcenter_clusters:
        if any(nbc2.vcenter_persistent_id == vc2.vcenter_persistent_id for nbc2 in netbox_clusters):
            logger.info(f"Cluster: {vc2.name} with vCenter_ID: {vc2.vcenter_persistent_id} exists in netbox, nothing to do")
        else:
            logger.info(f"Cluster: {vc2.name} with vCenter_ID: {vc2.vcenter_persistent_id} does NOT exists in netbox, adding the cluster to netbox")

            try:
                # Get the cluster type for vsphere
                cluster_type = netbox_client.virtualization.cluster_types.get(name="vSphere")

                nbc2_create = netbox_client.virtualization.clusters.create( name = vc2.name,
                                                                            type = cluster_type.id)
                logger.info("Cluster object created successfully in netbox")
            except Exception as ex:
                logger.warn("Failed creating the cluster object in netbox")
                logger.exception(ex)
                
def get_vcenter_vms():
    vcenter_vms = []
    
    vmsView = vcenter_content.viewManager.CreateContainerView( vcenter_content.rootFolder, [vim.VirtualMachine], True )
    
    for vm in vmsView.view:
        logging.info(f"Gathering information about VM: {vm.name}")
        a = datetime.datetime.now()
        # instanceUuid is only unique per vcenter, we need to combine it with the vcenter uuid 
        # if we want to be supporting multiple vcenters, currently we dont.

        # About 90ms faster per call, to cache to local variables first
        vm_config = vm.config
        vm_summary_config = vm.summary.config
        vm_guest = vm.guest
        vm_runtime = vm.runtime

        uuid = vm_config.instanceUuid
        vcpus = vm_summary_config.numCpu
        memory_mb = vm_summary_config.memorySizeMB
        comment = vm_config.annotation
        is_template = vm_config.template
        power_state = vm_runtime.powerState
        vmtools_status = vm_guest.toolsRunningStatus
        # TODO: Add disk usage

        # The API for getting _all_ ips are broken, the limit seems to be around 4 IP addresses are being returned
        # So for now, just take whatever IP is listed as the default, and figure out a way to fix it later on
        primary_ipaddress = vm_guest.ipAddress

        logging.debug(f"uuid: {uuid}, vcpus: {vcpus}, memory: {memory_mb}, comment: {comment}, is_template: {is_template}, power_state: {power_state}, vmtools_status: {vmtools_status}, primary_ip: {primary_ipaddress}")

        custom_attributes = {}
        vm_availablefield = vm.availableField
        for x in vm.customValue:
            fieldname = _vcenter_get_customfield_fieldname(vm_availablefield, x)
            custom_attributes[fieldname] = x.value
        
        # cluster_name = vm.summary.runtime.host.parent.name
        cluster_name = _vcenter_get_clustername(vm_runtime.host)

        # This didn't really return all ips, only up to 4 ips, so not optimal, only the vcenter ui api endpoint returns the correct info
        # Just here for future reference
        #
        # print(f"\t { len(vm.guest.net) }")
        # for nic in vm.guest.net:
        #     nic_ipconfig = nic.ipConfig
        #     if nic_ipconfig is not None:
        #         addresses = nic_ipconfig.ipAddress
        #         # print(len( nic.ipConfig.ipAddress))
        #         if len( nic.ipConfig.ipAddress) >= 4:
        #             print(f"VM: {vm.name} has 4 or more ip addresses defined, we are probably not getting all ips from the API")
            # for adr in addresses:
            #     print(adr.ipAddress)
            # debug_print_object_info(nic)
            # raise SystemExit(-1)

        vcenter_vms.append( VMwareVM( name = vm.name,
                                      uuid = uuid,
                                      vcpu = vcpus,
                                      memory_mb = memory_mb,
                                      comment = comment,
                                      power_state = power_state,
                                      vmtools_status = vmtools_status,
                                      primary_ipaddress = primary_ipaddress,
                                      is_template = is_template,
                                      custom_attributes = custom_attributes,
                                      cluster_name = cluster_name ) )

        b = datetime.datetime.now()
        c = b - a
        logger.info(f"Time per vm: {c.microseconds / 1000 } ms")

    return vcenter_vms

@functools.lru_cache(maxsize=32)
def _vcenter_get_clustername(host):
    # This function response is cached, since we will call it often
    return str(host.parent.name)

def _netbox_get_cluster_id(netbox_clusters, vcenter_cluster_name):
    for cluster in netbox_clusters:
        if cluster.raw_netbox_api_record.name == vcenter_cluster_name:
            return cluster.raw_netbox_api_record.id

def _vcenter_get_customfield_fieldname(available_fields, custom_field):
    for x in available_fields:
        if x.key == custom_field.key:
            return x.name

def get_netbox_vms():
    netbox_vms = []

    try:
        nb_vms = netbox_client.virtualization.virtual_machines.all()
    except Exception as ex: 
        logger.error("Failed getting a list of netbox vms")
        logger.exception(ex)
        raise SystemExit(-1)
    
    for nb_vm in nb_vms:
        netbox_vms.append( NetboxVM( name = nb_vm.name,
                                                   vcenter_persistent_id = nb_vm.custom_fields.get('vcenter_persistent_id'),
                                                   raw_netbox_api_record = nb_vm ) )
    
    return netbox_vms

def debug_print_object_info(obj):
    print(">x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>")
    for attr in dir(obj):
        print("obj.%s = %r" % (attr, getattr(obj, attr)))
    print("<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<")

def debug_print_netbox_object(obj):
    print(">x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>x>")
    pprint(dict(obj))
    print("<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<z<")

def initialize_vcenter_connection():
    global vcenter_session
    global vcenter_content

    vcenter_hostname = os.environ.get("VCENTER_HOSTNAME")
    vcenter_username = os.environ.get("VCENTER_USERNAME")
    vcenter_password = os.environ.get("VCENTER_PASSWORD")

    if not vcenter_hostname or not vcenter_username or not vcenter_password:
        logger.error("vCenter hostname/username/password is not set via environment variables")
        raise SystemExit(-1)

    vcenter_session = connect.SmartConnectNoSSL( host=vcenter_hostname,
                                                 user=vcenter_username,
                                                 pwd=vcenter_password,
                                                 port=int(443) )

    atexit.register(connect.Disconnect, vcenter_session)

    vcenter_content = vcenter_session.RetrieveContent()

def initialize_netbox_client():
    global netbox_client

    netbox_url = os.environ.get("NETBOX_API_URI")
    netbox_token = os.environ.get("NETBOX_API_TOKEN")

    if not netbox_url or not netbox_token:
        logger.error("Netbox url/token is not set via environment variables")
        raise SystemExit(-1)

    netbox_client = pynetbox.api (
        url = netbox_url,
        token = netbox_token,
        ssl_verify = False
    )

def initialize_logging():
    global logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    
    logger.addHandler(ch)
    
def main():
    initialize_logging()

    # Disable warnings about SSL
    urllib3.disable_warnings()
    
    initialize_vcenter_connection()
    initialize_netbox_client()

    update_netbox_clusters()

if __name__ == "__main__":
    main()

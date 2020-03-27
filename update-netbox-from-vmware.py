#!/usr/bin/python3
import pynetbox
import atexit
import ipaddress
import urllib3
import logging 
import os
import functools

from pyVim import connect
from pyVmomi import vmodl
from pyVmomi import vim
from pprint import pprint

vcenter_session = None
vcenter_content = None
netbox_client = None
logger = None

vcenter_vms = []
vcenter_clusters = []
netbox_vms = []
netbox_clusters = []
netbox_interfaces = []

class VMwareCluster:
    def __init__(self, name, vcenter_persistent_id, hosts):
        self.name = name
        self.vcenter_persistent_id = vcenter_persistent_id
        self.hosts = hosts

class NetboxCluster:
    def __init__(self, name, vcenter_persistent_id, raw_netbox_api_record):
        self.name = name
        self.vcenter_persistent_id = vcenter_persistent_id
        self.raw_netbox_api_record = raw_netbox_api_record

class GenericVM:
    def __init__(self, name, persistent_id, vcpu, memory_mb, disk_gb, comment, nics = None):
        self.name = name
        self.persistent_id = persistent_id
        self.vcpu = int(vcpu)
        self.memory_mb = int(memory_mb)
        self.disk_gb = int(disk_gb)
        self.comment = comment
        if nics is None:
            nics = []
        self.nics = nics

    def __repr__(self):
        return str.format("{{name: {0}, persistent_id: {1}, nics: {2}, vcpu: {3}, memory_mb: {4}, disk_gb: {5}, comment: {6} }}", 
            self.name,
            self.persistent_id,
            self.nics,
            self.vcpu,
            self.memory_mb,
            self.disk_gb,
            self.comment)

    def __eq__(self, other):
        if isinstance(other, GenericVM):
            return (self.name == other.name and
                   self.persistent_id == other.persistent_id and
                   self.nics == other.nics and
                   self.vcpu == other.vcpu and
                   self.memory_mb == other.memory_mb and
                   self.disk_gb == other.disk_gb and 
                   self.comment == other.comment)
        return False

class GenericNetworkInterface:
    def __init__(self, name, mac_address, connected, ip_addresses = None):
        self.name = name
        self.connected = connected
        self.mac_address = str(mac_address).upper()
        if ip_addresses is None:
            ip_addresses = []
        self.ip_addresses = ip_addresses
    
    def __repr__(self):
        return str.format("{{name: {0}, mac_address: {1}, connected: {2}, ip_addresses: {3} }}",
            self.name,
            self.mac_address,
            self.connected,
            self.ip_addresses)

    def __eq__(self, other):
        if isinstance(other, GenericNetworkInterface):
            return ( self.name == other.name and
                     self.connected == other.connected and
                     self.mac_address == other.mac_address and
                     self.ip_addresses == other.ip_addresses)
        return False

class VMwareVM:
    def __init__(self, name, uuid, vcpu, memory_mb, disk_gb, comment, power_state, vmtools_status, nics, primary_ipaddress, is_template, custom_attributes, cluster_name ):
        self.name = name
        self.uuid = uuid
        self.vcpu = vcpu
        self.memory_mb = memory_mb
        self.disk_gb = int(disk_gb)
        self.comment = comment
        self.power_state = power_state
        self.vmtools_status = vmtools_status
        self.nics = nics
        self.primary_ipaddress = primary_ipaddress
        self.is_template = is_template
        self.custom_attributes = custom_attributes
        self.cluster_name = cluster_name

class NetboxVM:
    def __init__(self, name, vcenter_persistent_id, raw_netbox_api_record):
            self.name = name
            self.vcenter_persistent_id = vcenter_persistent_id
            self.raw_netbox_api_record = raw_netbox_api_record

class NetboxInterface:
    def __init__(self, raw_netbox_api_record, netbox_vm_id):
            self.raw_netbox_api_record = raw_netbox_api_record
            self.netbox_vm_id = netbox_vm_id

@functools.lru_cache(maxsize=32)
def get_vcenter_clusters():
    global vcenter_clusters
    
    # Get a list of datacenters in the vcenter
    vcenter_datacenters = vcenter_content.rootFolder.childEntity

    # We only have one datacenter, but lets loop through the list of them anyway
    for vcenter_datacenter in vcenter_datacenters:
        for vcenter_cluster in vcenter_datacenter.hostFolder.childEntity:
            hosts = [ x._moId for x in vcenter_cluster.host ]
            vcenter_clusters.append( VMwareCluster( name = vcenter_cluster.name, 
                                                    vcenter_persistent_id = vcenter_cluster._moId,
                                                    hosts = hosts ) )

    return vcenter_clusters

def get_netbox_clusters():
    global netbox_clusters

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

def get_netbox_interfaces():
    global netbox_interfaces

    try:
        nb_interfaces = netbox_client.virtualization.interfaces.all()
    except Exception as ex: 
        logger.error("Failed getting a list of netbox interfaces")
        logger.exception(ex)
        raise SystemExit(-1)
    
    for nb_interface in nb_interfaces:
        netbox_interfaces.append( NetboxInterface( raw_netbox_api_record = nb_interface,
                                                   netbox_vm_id = nb_interface.virtual_machine.id ) )

def update_netbox():

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
                custom_fields = {}
                custom_fields["vcenter_persistent_id"] = vc2.vcenter_persistent_id
                
                nbc2_create = netbox_client.virtualization.clusters.create( name = vc2.name,
                                                                            type = cluster_type.id,
                                                                            custom_fields = custom_fields )
                logger.info("Cluster object created successfully in netbox")
            except Exception as ex:
                logger.warn("Failed creating the cluster object in netbox")
                logger.exception(ex)

    vcenter_vms = get_vcenter_vms()
    netbox_vms = get_netbox_vms()
    netbox_interfaces = get_netbox_interfaces()

    # Find VMs present in netbox, but not in vsphere, and add comment about it, on the netbox cluster object.
    # And update existing vms with latest information from vcenter if they already exists, and something has changed.
    for nbvm1 in netbox_vms:
        if any(vcvm1.uuid == nbvm1.vcenter_persistent_id for vcvm1 in vcenter_vms):
            logger.info(f"VM: {nbvm1.name} with vCenter_ID: {nbvm1.vcenter_persistent_id} exists in vcenter, checking if anything has changed")
            
            # next will raise errors if not found!, but we check above with any() so it is safe here
            vcvm = next(x for x in vcenter_vms if x.uuid == nbvm1.vcenter_persistent_id)

            # Convert the netbox and vcenter VM objects into a base VM, we can compare to each other etc.
            nb_basevm = _get_basevm_from_netbox_vm(nbvm1)
            vc_basevm = _get_basevm_from_vcenter_vm(vcvm)

            # Check if there is any differences between the vcenter/netbox VM object, bail early, if they are equal
            if nb_basevm == vc_basevm:
                logger.warn(f"The VM object: {nb_basevm.name} in both Netbox and vcenter looks the same, skipping early since there is no change.")
                continue

            # Figure out what exactly changed between netbox <> vcenter for the VM, and update accordingly
            try:
                vcpu_Changed = False
                memory_mb_Changed = False
                comment_Changed = False
                disk_Changed = False
                custom_fields_Changed = False
                
                if vc_basevm.vcpu != nb_basevm.vcpu:
                    vcpu_Changed = True
                    logger.info(f"Found change, VC VM vcpu: {vc_basevm.vcpu}, NB VM vcpu: {nb_basevm.vcpu}")
                elif vc_basevm.memory_mb != nb_basevm.memory_mb:
                    memory_mb_Changed = True
                    logger.info(f"Found change, VC VM memory: {vc_basevm.memory_mb}, NB VM memory: {nb_basevm.memory_mb}")
                elif vc_basevm.comment != nb_basevm.comment and nb_basevm.comment is not None:
                    comment_Changed = True
                    logger.info(f"Found change, VC VM comment: {vc_basevm.comment}, NB VM comment: {nb_basevm.comment}")
                elif nb_basevm.disk_gb is None or vc_basevm.disk_gb != nb_basevm.disk_gb:
                    disk_Changed = True
                    logger.info(f"Found change, VC VM disk size (GB): {vc_basevm.disk_gb}, NB VM disk size (GB): {nb_basevm.disk_gb}")
                
                # Our company specific custom netbox attribute, if it's defined, ignored otherwise
                # TODO: This should be optimized better to handle any custem fields, not just our own
                if "SystemID" in vcvm.custom_attributes and "SystemID" in nbvm1.raw_netbox_api_record.custom_fields:
                    vc_SystemID = vcvm.custom_attributes["SystemID"]
                    nb_SystemID = nbvm1.raw_netbox_api_record.custom_fields["SystemID"]
    
                    # Strip whitespaces in case the vcenter returns an empty string
                    if vc_SystemID != nb_SystemID and len(vc_SystemID.strip()) > 0:
                        custom_fields_Changed = True
                        logger.info(f"Found change, VC VM SystemID: {vc_SystemID}, NB VM SystemID: {nb_SystemID}")

                # This whole code is too complicated, figure out a better way
                # # Check if the VM is set to sync interfaces automatically, if so, check if we need to update anything
                # if nbvm1.raw_netbox_api_record.custom_fields["interface_sync_enabled"] is not None and nbvm1.raw_netbox_api_record.custom_fields["interface_sync_enabled"] == True:
                #     logger.info(f"VM: {nbvm1.name}, We should update this VMs interfaces if they are changed")
                    
                #     # Get the existing netbox interfaces for this VM
                #     nb_vm_interfaces = []
                #     for x1 in netbox_interfaces:
                #         if int(x1.raw_netbox_api_record.virtual_machine.id) == int(nbvm1.raw_netbox_api_record.id):
                #             nb_vm_interfaces.append( x1.raw_netbox_api_record )
                #     # logger.warn(f"arr size: {len(nb_vm_interfaces) } cnt = {nb_vm_interfaces} ")
 
                #     # Look at returned nics from vcenter
                #     for nic1 in vcvm.nics:
                #         found_nb_interface = False
                #         for nic2 in nb_vm_interfaces:
                #             logger.warn(f" nic1 type: { type(nic1)}, nic1 name: { nic1['label'] }")
                #             logger.warn(f" nic2 type: { type(nic2)}, nic2 name: { nic2.name }")
                #             if nic1['label'] == nic2.name:
                #                 logger.info(f"Found interface in both NB and VC, checking if there is any changes to associated IPs")
                #                 found_nb_interface = True

                #                 # We found an existing interface, check if the associated IP's match each other
                #                 try:
                #                     print(nic2.virtual_machine.id)
                #                     nb_ips = netbox_client.ipam.ip_addresses.filter(virtual_machine_id = nic2.virtual_machine.id )
                #                     # debug_print_object_info(nb_ips)
                #                     if nb_ips is not None:
                #                         for nb_ip in nb_ips:
                #                             found_nb_ip = False
                #                             for vc_ip in nic1["ipAddresses"]:
                #                                 if vc_ip == nb_ip.address:
                #                                     logger.info("Found IP in both VC and NB, nothing to do")
                #                                     found_nb_ip = True
                #                             if found_nb_ip is not True:
                #                                 logger.info("Did not find IP in NB")
                #                             debug_print_netbox_object(nb_ip)

                #                 except Exception as ex2:
                #                     logger.warn("Failed retrieving IPs for the VM object in netbox")
                #                     logger.exception(ex2)

                #                 debug_print_netbox_object(nic2)

                #         if found_nb_interface is not True:
                #             logger.info(f"We did not find the interface in NB, creating it")
                #             # We have a new interface in VC, that isnt in netbox, create it
                    
                #     for nic3 in nb_vm_interfaces:
                #         found_vc_interface = False
                #         for nic4 in vcvm.nics:
                #             if nic3.name == nic4['label']:
                #                 found_vc_interface = True

                #         if found_vc_interface is not True:
                #             logger.info(f"We did not find the interface in VC, delete it from netbox")
                #             # We did not find the NB interface in vcenter, delete it

                # TODO: Set primary IPs on the VM if they are changed, rest is defined on the interface associated to the VM

                if vcpu_Changed or memory_mb_Changed or comment_Changed or disk_Changed or custom_fields_Changed:
                    logger.info(f"Updating VM: {nbvm1.name} in netbox, since changes was detected!")
                    
                    nbvm1_update = netbox_client.virtualization.virtual_machines.get(nbvm1.raw_netbox_api_record.id)

                    if vcpu_Changed:
                        nbvm1_update.vcpus = vcvm.vcpu
                    if memory_mb_Changed:
                        nbvm1_update.memory = vcvm.memory_mb
                    if comment_Changed:
                        nbvm1_update.comments = vcvm.comment
                    if disk_Changed:
                        nbvm1_update.disk = vcvm.disk_gb
                    if custom_fields_Changed:
                        nbvm1_update.custom_fields["SystemID"] = vcvm.custom_attributes["SystemID"]

                    if nbvm1_update.save():
                        logger.info("Successfully updated VM object in netbox")
                else:
                    logger.info(f"No changes detected for VM: { nbvm1.name }")
            except Exception as ex:
                logger.warn("Failed updating the VM object in netbox")
                logger.exception(ex)
        else:
            logger.info(f"VM: {nbvm1.name} with vCenter_ID: {nbvm1.vcenter_persistent_id} does NOT exists in vcenter, adding comment to netbox")
            
            try:
                nbvm1_update = netbox_client.virtualization.virtual_machines.get(nbvm1.raw_netbox_api_record.id)
                nbvm1_update.comments = "No longer present in vCenter, verify manually, and delete this object in netbox"
                if nbvm1_update.save():
                    logger.info("Successfully updated VM object in netbox")
            except Exception as ex:
                logger.warn("Failed updating the VM object in netbox")
                logger.exception(ex)

    # Find vms present in vcenter, but not in netbox
    for vcvm2 in vcenter_vms:
        if any(nbvm2.vcenter_persistent_id == vcvm2.uuid for nbvm2 in netbox_vms):
            logger.info(f"VM: {vcvm2.name} with vCenter_ID: {vcvm2.uuid} exists in netbox, nothing to do")
        else:
            logger.info(f"VM: {vcvm2.name} with vCenter_ID: {vcvm2.uuid} does NOT exists in netbox, adding the cluster to netbox")

            try:
                netbox_cluster_id = _netbox_get_cluster_id(netbox_clusters, vcvm2.cluster_name)
                custom_fields = {}
                custom_fields["vcenter_persistent_id"] = vcvm2.uuid
                custom_fields["interface_sync_enabled"] = True
                
                comment = ""
                if vcvm2.comment is not None:
                    comment = vcvm2.comment

                # Our company specific custom netbox attribute, if it's defined
                if "SystemID" in vcvm2.custom_attributes:
                    custom_fields["SystemID"] = vcvm2.custom_attributes["SystemID"]
                
                # Create the VM object in netbox
                nbvm2_create = netbox_client.virtualization.virtual_machines.create( name = vcvm2.name,
                                                                                     cluster = netbox_cluster_id,
                                                                                     comments = comment,
                                                                                     custom_fields = custom_fields,
                                                                                     vcpus = vcvm2.vcpu,
                                                                                     memory = vcvm2.memory_mb )
                logger.info(f"VM object: { nbvm2_create } created successfully in netbox")
                
                # Create a new interface for each virtual nic for the VM in netbox:
                for nic in vcvm2.nics:
                    nb_interface_create = netbox_client.virtualization.interfaces.create( name = nic["label"],
                                                                                          type = "virtual",
                                                                                          mac_address = nic["macAddress"],
                                                                                          virtual_machine = nbvm2_create.id )

                    # If we have any ip addresses from VMware tools, try and get each ip from netbox, 
                    # and connect it to the interface we just created, we dont create new IP addresses 
                    # that are not already present in netbox, as it should be the source of truth
                    for ip in nic["ipAddresses"]:
                        try:
                            netbox_ip = netbox_client.ipam.ip_addresses.get(address=ip.with_prefixlen)
                            if netbox_ip is not None:
                                logger.info(f"VM: {vcvm2.name}, will add ip: { netbox_ip.address } to nic with mac: {nic['macAddress']}")
                                
                                netbox_ip.interface = nb_interface_create.id
                                if netbox_ip.save():
                                    logger.info("Successfully updated interface IP")
                            else:
                                logger.info(f"Could not find ip address: { ip.with_prefixlen } in netbox")
                        except Exception as ex2:
                            logger.warn("Failed retrieving IP address from netbox")
                            logger.exception(ex2)

            except Exception as ex:
                logger.warn("Failed creating the VM object in netbox")
                logger.exception(ex)

def _get_basevm_from_netbox_vm(netbox_vm):
    nics = []
    for netbox_interface in netbox_interfaces:
        if int(netbox_interface.raw_netbox_api_record.virtual_machine.id) == int(netbox_vm.raw_netbox_api_record.id):
            ip_addresses = []
            nb_ips = netbox_client.ipam.ip_addresses.filter( virtual_machine_id = netbox_vm.raw_netbox_api_record.id)
            for nb_ip in nb_ips:
                if nb_ip.interface.id == netbox_interface.raw_netbox_api_record.id:
                    ip_addresses.append( nb_ip.address )

            nics.append( GenericNetworkInterface( name = netbox_interface.raw_netbox_api_record.name,
                                                  connected = netbox_interface.raw_netbox_api_record.enabled,
                                                  mac_address = netbox_interface.raw_netbox_api_record.mac_address,
                                                  ip_addresses = ip_addresses ) )

    base_vm = GenericVM( name = netbox_vm.name, 
                         persistent_id = netbox_vm.vcenter_persistent_id,
                         vcpu = netbox_vm.raw_netbox_api_record.vcpus,
                         memory_mb = netbox_vm.raw_netbox_api_record.memory,
                         disk_gb = netbox_vm.raw_netbox_api_record.disk,
                         comment = netbox_vm.raw_netbox_api_record.comments,
                         nics = nics )

    return base_vm

def _get_basevm_from_vcenter_vm(vcenter_vm):
    nics = []
    for nic in vcenter_vm.nics:
        ip_addresses = []
        if "ipAddresses" in nic:
            for ip in nic["ipAddresses"]:
                ip_addresses.append( str(ip) )

        nics.append( GenericNetworkInterface( name = nic['label'],
                                              connected = nic['connected'],
                                              mac_address = nic['macAddress'],
                                              ip_addresses = ip_addresses ) )

    base_vm = GenericVM( name = vcenter_vm.name, 
                         persistent_id = vcenter_vm.uuid,
                         vcpu = vcenter_vm.vcpu, 
                         memory_mb = vcenter_vm.memory_mb,
                         disk_gb = vcenter_vm.disk_gb,
                         comment = vcenter_vm.comment,
                         nics = nics )

    return base_vm

def get_vcenter_vms():
    global vcenter_vms

    vmsView = vcenter_content.viewManager.CreateContainerView( vcenter_content.rootFolder, [vim.VirtualMachine], True )

    vm_properties = [ "name", "config.instanceUuid", "summary.config.numCpu", "summary.config.memorySizeMB",
                      "config.annotation", "config.template", "runtime.powerState", "guest.toolsRunningStatus",
                      "guest.ipAddress", "summary.runtime.host", "availableField", "customValue", "config.hardware.device",
                      "guest.net" ]
    
    vm_data = _get_vcenter_vms(container_view=vmsView, vm_properties=vm_properties)

    for vm in vm_data:
        logging.info(f"Gathering information about VM: { vm['name'] }")
        # instanceUuid is only unique per vcenter, we need to combine it with the vcenter uuid 
        # if we want to be supporting multiple vcenters, currently we dont.

        uuid = vm["config.instanceUuid"]
        vcpus = vm["summary.config.numCpu"]
        memory_mb = vm["summary.config.memorySizeMB"]
        comment = vm.get("config.annotation") or "" # Might not exist
        comment = comment.rstrip()
        is_template = vm["config.template"]
        power_state = vm["runtime.powerState"]
        vmtools_status = vm["guest.toolsRunningStatus"]
        
        disk_size_gb = 0
        vm_nics = []
        for device in vm["config.hardware.device"]:
             if isinstance(device, vim.vm.device.VirtualDisk):
                disk_size_gb += (device.capacityInKB / 1024 / 1024)
             elif isinstance(device, vim.vm.device.VirtualEthernetCard):
                device_info = {}
                device_info["macAddress"] = device.macAddress
                device_info["label"] = device.deviceInfo.label
                device_info["connected"] = device.connectable.connected
                vm_nics.append( device_info )
        
        # If VMware tools are running, try to get the IPs reported back from the VMware tools
        # This is really buggy territory, even if VMware tools are running, they could return 
        # anything from nothing to wrong IPs, or anything else really depending on the version/os installed.
        # Some Linux versions of the VMware tools seems really bad (returning the same ips for all nics / 
        # interfaces present on the VM)
        if vmtools_status == "guestToolsRunning":
            logger.info(f"VM: { vm['name'] } - VMware Tools running, trying to get IPs reported back")

            for nic1 in vm["guest.net"]:
                for nic2 in vm_nics:
                    if nic2["macAddress"] == nic1.macAddress:
                        interface_addresses = []
                        if nic1.ipConfig is not None: # Might return nothing even if vmware tools are running
                            for addr in nic1.ipConfig.ipAddress:
                                logger.info(f"VM: {vm['name']}, nic: {addr.ipAddress}, mac: { nic1.macAddress }")
                                ip_address = ipaddress.ip_interface(f"{ addr.ipAddress }/{ addr.prefixLength }" )
                                interface_addresses.append(ip_address)
                            
                            nic2["ipAddresses"] = interface_addresses
        
        # The API for getting _all_ ips are broken, the limit seems to be around 4 IP addresses are being returned
        # So for now, just take whatever IP is listed as the default, and figure out a way to fix it later on
        # This might be somewhat related to the vmtools version installed, needs further investigation
        primary_ipaddress = vm.get("guest.ipAddress") or "" # Might not exist

        logging.debug(f"uuid: {uuid}, vcpus: {vcpus}, memory: {memory_mb}, comment: {comment}, is_template: {is_template}, power_state: {power_state}, vmtools_status: {vmtools_status}, primary_ip: {primary_ipaddress}, disksize: { disk_size_gb}")
        
        custom_attributes = {}
        vm_availablefield = vm["availableField"]
        for x in vm["customValue"]:
            fieldname = _vcenter_get_customfield_fieldname(vm_availablefield, x)
            custom_attributes[fieldname] = x.value
        
        cluster_name = _vcenter_get_clustername(vm['summary.runtime.host']._moId)

        vcenter_vms.append( VMwareVM( name = vm['name'],
                                      uuid = uuid,
                                      vcpu = vcpus,
                                      memory_mb = memory_mb,
                                      disk_gb = disk_size_gb,
                                      comment = comment,
                                      power_state = power_state,
                                      vmtools_status = vmtools_status,
                                      nics = vm_nics,
                                      primary_ipaddress = primary_ipaddress,
                                      is_template = is_template,
                                      custom_attributes = custom_attributes,
                                      cluster_name = cluster_name ) )

    return vcenter_vms

def _get_vcenter_vms(container_view, vm_properties):
  
    object_spec = vmodl.query.PropertyCollector.ObjectSpec( obj = container_view,
                                                            skip = True)

    traversal_spec = vmodl.query.PropertyCollector.TraversalSpec( name = 'traverseEntities',
                                                                  path = 'view',
                                                                  skip = False,
                                                                  type = container_view.__class__ )

    object_spec.selectSet = [traversal_spec]

    property_spec = vmodl.query.PropertyCollector.PropertySpec( type = vim.VirtualMachine,
                                                                pathSet = vm_properties )

    filter_spec = vmodl.query.PropertyCollector.FilterSpec( objectSet = [object_spec],
                                                            propSet = [property_spec] )

    vm_properties = vcenter_content.propertyCollector.RetrieveContents([filter_spec])

    vm_data = []
    for vm_property in vm_properties:
        properties = {}
        for prop in vm_property.propSet:
            properties[prop.name] = prop.val
            properties['obj'] = vm_property.obj

        vm_data.append(properties)
    return vm_data

@functools.lru_cache(maxsize=32)
def _vcenter_get_clustername(host):
    for cluster in vcenter_clusters:
        for cluster_host in cluster.hosts:
            if cluster_host.endswith(host):
                return cluster.name

    return None

def _netbox_get_cluster_id(netbox_clusters, vcenter_cluster_name):
    for cluster in netbox_clusters:
        if cluster.raw_netbox_api_record.name == vcenter_cluster_name:
            return cluster.raw_netbox_api_record.id

def _vcenter_get_customfield_fieldname(available_fields, custom_field):
    for x in available_fields:
        if x.key == custom_field.key:
            return x.name

def get_netbox_vms():
    global netbox_vms

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
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    
    logger.addHandler(ch)
    
def main():
    initialize_logging()

    # Disable warnings about SSL
    urllib3.disable_warnings()
    
    initialize_vcenter_connection()
    initialize_netbox_client()

    get_vcenter_clusters()
    get_vcenter_vms()
    get_netbox_clusters()
    get_netbox_vms()
    get_netbox_interfaces()

    update_netbox()

if __name__ == "__main__":
    main()

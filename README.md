Script to update netbox with vsphere VMs

I didn't like the options / existing scripts I found, so decided to create my own instead

We dont push IP addresses that doesn't already exist in Netbox, as Netbox should be the source of truth for the desired network state.

It doesn't assume much about the vcenter/netbox setup, other than you need to add a custom field to netbox, so we have a unique id we can use when updating the netbox objects, in case a vms get renamed etc. We also have a second custom field, that controls whether we should update a VMs interfaces automatically, it defaults to false, but new VMs created by the script will be set to true.

You can create the necessary custom fields in Netbox:
# VMware Persistent ID
1. Netbox Administration -> Extras -> Custom fields -> Add 
2. Select `virtualization->cluster` and `virtualization->virtual machine` in Object(s)
3. Set type to `text`
4. Set name to `vcenter_persistent_id`
5. Filter logic: Disabled

# Interface Sync Enabled
1. Netbox Administration -> Extras -> Custom fields -> Add 
2. Select `virtualization->virtual machine` in Object(s)
3. Set type to `boolean`
4. Set name to `interface_sync_enabled`
5. Set default to false

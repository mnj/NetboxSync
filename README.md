Script to update netbox with vsphere VMs

I didn't like the options / existing scripts I found, so decided to create my own instead

It doesn't assume much about the vcenter/netbox setup, other than you need to add a custom field to netbox, so we have a unique id we can use when updating the netbox objects.

You can create the necessary custom field in Netbox:
1. Netbox Administration -> Extras -> Custom fields -> Add 
2. Select "virtualization->cluster" and "virtualization->virtual machine" in Object(s)
3. Set type to text
4. Set name to `vcenter_persistent_id`
5. Filter logic: Disabled

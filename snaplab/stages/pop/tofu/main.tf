# The lab's virtual machines (Design 0001, D7): the OPNsense router and the
# Ubuntu machines, cloned from the templates CRACKLE and POP made.
#
# POP writes lab.auto.tfvars.json from snap.yaml and applies this in
# /var/lib/snap/tofu. To change the lab later, edit the variables and run
# `tofu apply` there again.

terraform {
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.116.0"
    }
  }
}

variable "node" { type = string }
variable "api_token_file" { type = string }
variable "lab_bridge" { type = string }
variable "vm_storage" { type = string }

variable "router" {
  type = object({
    vmid       = number
    name       = string
    template   = number
    config_iso = string # volume id of the bootstrap config ISO
    started    = bool   # false until POP is watching the console for the importer
    memory_mb  = number
    cpu        = number
    uplink     = string # bridge of the WAN card
    admin_vlan = number
    lab_vlans  = list(number) # one card per lab network, in snap.yaml order; 0 = untagged
  })
}

variable "machines" {
  type = list(object({
    vmid      = number
    name      = string
    template  = number
    cpu       = number
    memory_mb = number
    disk_gb   = number
    vlan      = number # 0 = untagged
    mac       = string # fixed, so the router can reserve its address
  }))
}

variable "username" { type = string }
variable "password_hash" {
  type    = string
  default = null
}
variable "ssh_keys" { type = list(string) }

provider "proxmox" {
  endpoint  = "https://127.0.0.1:8006/"
  api_token = trimspace(file(var.api_token_file))
  insecure  = true # the host's own self-signed certificate
}

resource "proxmox_virtual_environment_vm" "router" {
  node_name   = var.node
  vm_id       = var.router.vmid
  name        = var.router.name
  description = "SNAP: lab router and firewall (OPNsense)"
  tags        = ["snap"]
  started     = var.router.started
  on_boot     = true

  clone {
    vm_id = var.router.template
    full  = true
  }

  cpu {
    cores = var.router.cpu
    type  = "host"
  }
  memory {
    dedicated = var.router.memory_mb
  }

  cdrom {
    interface = "ide2"
    file_id   = var.router.config_iso
  }

  # vtnet0: WAN, vtnet1: admin network, vtnet2...: the lab networks.
  network_device {
    bridge = var.router.uplink
  }
  network_device {
    bridge  = var.lab_bridge
    vlan_id = var.router.admin_vlan
  }
  dynamic "network_device" {
    for_each = var.router.lab_vlans
    content {
      bridge  = var.lab_bridge
      vlan_id = network_device.value == 0 ? null : network_device.value
    }
  }
}

resource "proxmox_virtual_environment_vm" "machine" {
  for_each = { for m in var.machines : m.name => m }

  node_name   = var.node
  vm_id       = each.value.vmid
  name        = each.value.name
  description = "SNAP: lab machine"
  tags        = ["snap"]
  on_boot     = true

  clone {
    vm_id = each.value.template
    full  = false # linked clone: fast and small
  }

  cpu {
    cores = each.value.cpu
  }
  memory {
    dedicated = each.value.memory_mb
  }
  disk {
    datastore_id = var.vm_storage
    interface    = "scsi0"
    size         = each.value.disk_gb
  }

  network_device {
    bridge      = var.lab_bridge
    vlan_id     = each.value.vlan == 0 ? null : each.value.vlan
    mac_address = each.value.mac
  }

  initialization {
    datastore_id = var.vm_storage
    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
    user_account {
      username = var.username
      password = var.password_hash
      keys     = var.ssh_keys
    }
  }
}

output "machines" {
  value = { for name, vm in proxmox_virtual_environment_vm.machine : name => vm.vm_id }
}

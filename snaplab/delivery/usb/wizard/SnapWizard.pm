package SnapWizard;

# The setup wizard's work outside the screen (Design 0001, section 5.3): what
# the computer has (disks, network ports, hardware checks, time zones), and
# turning the wizard's choices into the installer's answer file and the
# settings SNAP continues with after the restart (/etc/snap/wizard.json).
#
# Plain Perl with JSON::PP, so it runs in the Proxmox installer and in tests.

use strict;
use warnings;
use JSON::PP;

our $JSON = JSON::PP->new->utf8->canonical->allow_nonref;

sub slurp {
    my ($path) = @_;
    open(my $fh, '<', $path) or return undef;
    local $/;
    my $text = <$fh>;
    close($fh);
    return $text;
}

sub trim { my ($s) = @_; $s //= ''; $s =~ s/^\s+|\s+$//g; $s =~ s/\s+/ /g; return $s; }

# Internal disks: never USB (the stick, card readers, external drives), never the stick itself.
sub disks {
    my ($sys_block, $stick) = @_;
    $stick //= '';
    my @found;
    opendir(my $dh, $sys_block) or return [];
    for my $name (sort readdir($dh)) {
        next if $name =~ /^\./ || $name =~ /^(loop|ram|zram|sr|fd|md|dm-|nbd)/ || $name =~ /^mmcblk\d+boot/;
        next if $name eq $stick;
        my $dir = "$sys_block/$name";
        my $size = trim(slurp("$dir/size")) || 0;
        next unless $size > 0;
        my $device = readlink("$dir/device") // '';
        my $real = -e "$dir/device" ? (eval { require Cwd; Cwd::abs_path("$dir/device") } // $device) : $device;
        next if $real =~ m{/usb};
        push @found, {
            name => $name,
            model => trim(slurp("$dir/device/model")) || 'Disk',
            gb => int($size * 512 / 1000000000),
        };
    }
    closedir($dh);
    return \@found;
}

# Physical network ports (with a device behind them), and whether a cable is connected.
sub interfaces {
    my ($sys_net) = @_;
    my @found;
    opendir(my $dh, $sys_net) or return [];
    for my $name (sort readdir($dh)) {
        next if $name =~ /^\./ || $name eq 'lo' || !-e "$sys_net/$name/device";
        push @found, {
            name => $name,
            mac => trim(slurp("$sys_net/$name/address")),
            carrier => (trim(slurp("$sys_net/$name/carrier")) eq '1') ? JSON::PP::true : JSON::PP::false,
        };
    }
    closedir($dh);
    return \@found;
}

# Pass / Fix / Note lines for the hardware check screen. Fix items stop setup.
sub checks {
    my (%in) = @_;    # disks, interfaces, cpuinfo, meminfo
    my @out;
    my @disks = @{ $in{disks} };
    if (@disks) {
        my ($big) = sort { $b->{gb} <=> $a->{gb} } @disks;
        push @out, { level => 'pass', text => scalar(@disks) . " internal disk(s) found, the largest $big->{gb} GB" };
    } else {
        push @out, { level => 'fix', text => 'No internal disk was found. Check that the disk is connected and turned on in the computer\'s setup screen.' };
    }
    if (($in{cpuinfo} // '') =~ /^flags\s*:.*\b(vmx|svm)\b/m) {
        push @out, { level => 'pass', text => 'Virtualization is turned on' };
    } else {
        push @out, { level => 'fix', text => 'Virtualization is turned off. Turn on Intel VT-x or AMD-V in the computer\'s setup screen (often F2 or Delete at start-up), then start from the USB stick again.' };
    }
    my ($kb) = ($in{meminfo} // '') =~ /^MemTotal:\s+(\d+)/m;
    my $gb = int(($kb // 0) / 1024 / 1024 + 0.5);
    if ($gb >= 12) {
        push @out, { level => 'pass', text => "Memory: $gb GB" };
    } elsif ($gb >= 4) {
        push @out, { level => 'note', text => "This computer has $gb GB of memory. The built-in lab needs about 8 GB; with less, choose fewer or smaller machines." };
    } else {
        push @out, { level => 'fix', text => "This computer has only $gb GB of memory; SNAP needs at least 4 GB." };
    }
    if (grep { $_->{carrier} } @{ $in{interfaces} }) {
        push @out, { level => 'pass', text => 'Network cable connected' };
    } else {
        push @out, { level => 'note', text => 'No network cable is connected. Setup works without one, but the lab will have no internet.' };
    }
    return \@out;
}

sub timezones {
    my ($zoneinfo) = @_;
    my @zones = ('UTC');
    for my $region (qw(Africa America Antarctica Asia Atlantic Australia Europe Indian Pacific)) {
        my @stack = ("$zoneinfo/$region");
        while (my $dir = shift @stack) {
            opendir(my $dh, $dir) or next;
            for my $entry (sort readdir($dh)) {
                next if $entry =~ /^\./;
                my $path = "$dir/$entry";
                if (-d $path) { push @stack, $path; next }
                (my $zone = $path) =~ s{^\Q$zoneinfo\E/}{};
                push @zones, $zone;
            }
            closedir($dh);
        }
    }
    return [ 'UTC', sort grep { $_ ne 'UTC' } @zones ];
}

sub hash_password {
    my ($password) = @_;
    my @chars = ('a' .. 'z', 'A' .. 'Z', 0 .. 9, '.', '/');
    my $salt = join('', map { $chars[ int(rand(@chars)) ] } 1 .. 16);
    my $hash = crypt($password, "\$6\$$salt\$");
    die "this system cannot make SHA-512 password hashes\n" unless $hash && $hash =~ /^\$6\$/;
    return $hash;
}

sub _q { return $JSON->encode("$_[0]") }    # a JSON string is a valid TOML basic string

# The installer's answer file: the stick's own answer.toml (it has the first-boot
# handoff) with the wizard's choices put in.
sub answer_toml {
    my ($template, $req, $hash) = @_;
    my $h = $req->{host};
    my %global = (
        fqdn => _q("$h->{hostname}.$h->{domain}"),
        keyboard => _q($h->{keyboard}),
        timezone => _q($h->{timezone}),
        country => _q($h->{country}),
        'root-password-hashed' => _q($hash),
    );
    my @network;
    my $m = $h->{management} // { mode => 'dhcp' };
    if (($m->{mode} // 'dhcp') eq 'static') {
        @network = (
            'source = "from-answer"',
            'cidr = ' . _q($m->{address}),
            'gateway = ' . _q($m->{gateway}),
            'dns = ' . _q($m->{dns}[0]),
            # udev names a port enx<mac> by its MAC address, so this matches exactly one port.
            'filter.ID_NET_NAME_MAC = ' . _q('*' . join('', split(/:/, lc($m->{mac} // '')))),
        );
    } else {
        @network = ('source = "from-dhcp"');
    }
    my (@out, $section);
    for my $line (split /\n/, $template) {
        if ($line =~ /^\[(.+)\]\s*$/) {
            $section = $1;
            push @out, $line;
            push @out, @network if $section eq 'network';
            if ($section eq 'disk-setup') {
                push @out, 'disk-list = [' . _q($req->{disk}) . ']';
            }
            next;
        }
        next if defined $section && $section eq 'network' && $line =~ /\S/;
        if (defined $section && $section eq 'disk-setup' && $line =~ /^(disk-list|filter\.|filter_match)/) {
            next;
        }
        if (defined $section && $section eq 'global' && $line =~ /^([\w-]+)\s*=/ && exists $global{$1}) {
            $line = "$1 = $global{$1}";
        }
        push @out, $line;
    }
    return join("\n", @out) . "\n";
}

# The settings SNAP continues with after the restart: snap.yaml's own shape,
# laid over the stick's settings by the engine (snaplab/hostcli.py).
sub wizard_settings {
    my ($req, $hash) = @_;
    my %host = %{ $req->{host} };
    delete $host{management};
    $host{root_password_hash} = $hash;
    my %machines = %{ $req->{machines} };
    $machines{password_hash} = $hash;
    return { host => \%host, networks => $req->{networks}, machines => \%machines };
}

# One line of the installer's output: (percent, text) for a progress line, else ().
sub parse_progress {
    my ($line) = @_;
    return () unless $line =~ /progress\s+([\d.]+)\s*%(?:\s*-\s*(.*?))?\s*$/;
    return ($1 + 0, $2);
}

1;

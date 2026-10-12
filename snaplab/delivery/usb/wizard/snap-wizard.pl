#!/usr/bin/perl
# SNAP setup wizard window (Design 0001, section 5.3).
#
# Started by the Proxmox installer's graphical environment in place of
# Proxmox's own installer window (wizard-setup.sh writes /.xinitrc). Shows the
# wizard page full screen with the WebKit engine Proxmox's installer uses, and
# answers the page's "snap:" links:
#
#   snap:ready     SNAP.init(...) with the stick's settings and what this computer has
#   snap:recheck   the hardware check again
#   snap:install   runs this script with --install in the background and reports
#                  SNAP.progress / SNAP.installFailed / SNAP.installDone
#   snap:poweroff  turns the computer off
#
# When the install is done the window closes; the installer environment then
# restarts the computer into the new system.
#
#   snap-wizard.pl --install request.json   the install itself, one report per line:
#       P<TAB>percent<TAB>text   L<TAB>log line   FAIL<TAB>message   DONE

use strict;
use warnings;
use File::Basename qw(dirname);
use lib dirname(__FILE__);
use SnapWizard;

my $DIR = dirname(__FILE__);
my $ANSWER_TEMPLATE = $ENV{SNAP_ANSWER_TEMPLATE} // '/cdrom/answer.toml';
# The paths can be moved for tests (tests/test_wizard_perl.py).
my $ANSWERS = $ENV{SNAP_ANSWERS} // '/run/automatic-installer-answers';
my $TARGET = $ENV{SNAP_TARGET} // '/mnt/snap-target';
my $LOG = $ENV{SNAP_WIZARD_LOG} // '/tmp/snap-wizard.log';
my $SYS = $ENV{SNAP_SYS} // '/sys';

my $CONSOLE = $ENV{SNAP_WIZARD_CONSOLE} // '/dev/console';

# Each line also goes to the system console: hidden behind the window on a
# real screen, and in the serial log of a stick built with --serial-console.
sub logline {
    if (open(my $fh, '>>', $LOG)) { print $fh scalar(localtime), " $_[0]\n"; close($fh) }
    if (open(my $fh, '>>', $CONSOLE)) { print $fh "SNAP wizard: $_[0]\r\n"; close($fh) }
}

# --- the install (no window) ----------------------------------------------------

sub report { print join("\t", @_), "\n"; STDOUT->flush; logline(join(' ', @_)) }

sub run_install {
    my ($request_file) = @_;
    $| = 1;
    my $req = $SnapWizard::JSON->decode(SnapWizard::slurp($request_file) // die "no request\n");
    my $m = $req->{host}{management} // {};
    if (($m->{mode} // '') eq 'static') {
        $m->{mac} = SnapWizard::trim(SnapWizard::slurp("$SYS/class/net/$m->{iface}/address"));
    }
    my $hash = SnapWizard::hash_password($req->{password});
    my $template = SnapWizard::slurp($ANSWER_TEMPLATE) // return report('FAIL', "cannot read $ANSWER_TEMPLATE");
    open(my $out, '>', $ANSWERS) or return report('FAIL', "cannot write $ANSWERS: $!");
    print $out SnapWizard::answer_toml($template, $req, $hash);
    close($out);
    report('P', 1, 'Checking the disks and network ports');

    # The automatic installer reads the hardware from this cache (unconfigured.sh
    # makes it only in its own automatic mode).
    if (system("proxmox-low-level-installer dump-udev >>$LOG 2>&1") != 0) {
        return report('FAIL', 'Proxmox could not read the hardware (proxmox-low-level-installer dump-udev)');
    }

    report('P', 2, 'Starting the Proxmox installer');
    my $last_error = '';
    open(my $inst, '-|', "proxmox-auto-installer <$ANSWERS 2>&1") or return report('FAIL', "cannot start the installer: $!");
    while (my $line = <$inst>) {
        chomp $line;
        report('L', $line);
        $last_error = $line if $line =~ /error|failed/i;
        my ($pct, $text) = SnapWizard::parse_progress($line);
        report('P', 2 + $pct * 0.95, $text // '') if defined $pct;
    }
    close($inst);
    if ($? != 0) {
        return report('FAIL', $last_error || 'The Proxmox installer stopped (exit code ' . ($? >> 8) . ')');
    }

    # The rest of the wizard's choices go onto the new system for CRACKLE and POP.
    report('P', 98, 'Saving your lab settings');
    my $settings = SnapWizard::wizard_settings($req, $hash);
    system('vgchange -ay pve >/dev/null 2>&1');
    mkdir $TARGET;
    if (system("mount /dev/pve/root $TARGET") != 0) {
        return report('FAIL', 'Proxmox is installed, but its disk could not be opened to save the lab settings');
    }
    mkdir "$TARGET/etc/snap";
    chmod 0700, "$TARGET/etc/snap";
    for ([ 'wizard.json', $SnapWizard::JSON->pretty->encode($settings) ], [ 'password', "$req->{password}\n" ]) {
        my ($name, $content) = @$_;
        open(my $fh, '>', "$TARGET/etc/snap/$name") or return report('FAIL', "cannot save $name: $!");
        chmod 0600, "$TARGET/etc/snap/$name";
        print $fh $content;
        close($fh);
    }
    system("sync; umount $TARGET; vgchange -an pve >/dev/null 2>&1");
    report('P', 100, 'Done');
    report('DONE');
}

if (@ARGV && $ARGV[0] eq '--install') {
    run_install($ARGV[1]);
    exit 0;
}

# --- the window -----------------------------------------------------------------

require Glib;
Glib->import(qw(TRUE FALSE));
require Gtk3;
Gtk3->import('-init');
require Gtk3::WebKit2;

my $window = Gtk3::Window->new('toplevel');
$window->set_title('SNAP setup');
$window->set_default_size(1024, 768);
$window->signal_connect(destroy => sub { Gtk3->main_quit });
my $view = Gtk3::WebKit2::WebView->new;
$window->add($view);
$window->fullscreen;
$window->show_all;

sub js {
    my ($code) = @_;
    # run_javascript is the older name (WebKitGTK before 2.40); evaluate_javascript the newer.
    eval { $view->evaluate_javascript($code, -1, undef, undef, undef, undef, undef); 1 }
        or eval { $view->run_javascript($code, undef, undef, undef) };
}

sub call { my ($fn, @args) = @_; js("SNAP.$fn(" . join(',', map { $SnapWizard::JSON->encode($_) } @args) . ')') }

sub stick_device {
    for my $line (split /\n/, SnapWizard::slurp('/proc/mounts') // '') {
        my ($dev, $where) = split / /, $line;
        return ($dev =~ m{^/dev/(\w+?)(\d*)$})[0] if $where eq '/cdrom' || $where eq '/cdrom/';
    }
    return '';
}

sub computer {
    my $stick = stick_device();
    my $disks = SnapWizard::disks('/sys/block', $stick);
    my $nics = SnapWizard::interfaces('/sys/class/net');
    return {
        config => $SnapWizard::JSON->decode(SnapWizard::slurp("$DIR/config.json")),
        disks => $disks,
        interfaces => $nics,
        timezones => SnapWizard::timezones('/usr/share/zoneinfo'),
        checks => SnapWizard::checks(
            disks => $disks, interfaces => $nics,
            cpuinfo => SnapWizard::slurp('/proc/cpuinfo'), meminfo => SnapWizard::slurp('/proc/meminfo'),
        ),
        auto => ((SnapWizard::slurp('/proc/cmdline') // '') =~ /\bsnap\.wizard=auto\b/) ? JSON::PP::true : JSON::PP::false,
    };
}

sub start_install {
    my ($req) = @_;
    my $file = '/tmp/snap-wizard-request.json';
    open(my $fh, '>', $file) or return call('installFailed', "cannot write $file: $!");
    chmod 0600, $file;
    print $fh $SnapWizard::JSON->encode($req);
    close($fh);
    my $pid = open(my $out, '-|', $^X, __FILE__, '--install', $file);
    return call('installFailed', "cannot start the install: $!") unless $pid;
    my $finished = 0;
    Glib::IO->add_watch(fileno($out), [ 'in', 'hup' ], sub {
        my $line = <$out>;
        if (!defined $line) {
            close($out);
            call('installFailed', 'The install stopped unexpectedly; see /tmp/snap-wizard.log') unless $finished;
            return 0;
        }
        chomp $line;
        my ($kind, @rest) = split /\t/, $line;
        if ($kind eq 'P') { call('progress', $rest[0] + 0, $rest[1] // '') }
        elsif ($kind eq 'L') { call('progress', undef, undef, $rest[0] // '') }
        elsif ($kind eq 'FAIL') { $finished = 1; call('installFailed', $rest[0] // 'failed') }
        elsif ($kind eq 'DONE') {
            $finished = 1;
            call('installDone');
            Glib::Timeout->add(6000, sub { Gtk3->main_quit; 0 });
        }
        return 1;
    });
}

sub handle {
    my ($uri) = @_;
    my ($action, $query) = $uri =~ /^snap:(\w+)\??(.*)$/ or return;
    $query =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/ge;
    my $payload = length($query) ? eval { $SnapWizard::JSON->decode($query) } : {};
    logline("action $action");
    if ($action eq 'ready' || $action eq 'recheck') { call('init', computer()) }
    elsif ($action eq 'install') { start_install($payload) }
    elsif ($action eq 'poweroff') { system('poweroff', '-f') }
}

$view->signal_connect('decide-policy' => sub {
    my ($v, $decision, $type) = @_;
    my $uri = eval { $decision->get_navigation_action->get_request->get_uri }
        // eval { $decision->get_request->get_uri } // '';
    return 0 unless $uri =~ /^snap:/;
    $decision->ignore;
    Glib::Idle->add(sub { handle($uri); 0 });
    return 1;
});

$view->load_uri("file://$DIR/index.html");
Gtk3->main;

use strict;
use warnings;
use Cwd qw(getcwd);
use Digest::SHA qw(sha256_hex);
use Encode qw(decode encode FB_CROAK);
use Fcntl qw(:DEFAULT O_NOFOLLOW);
use JSON::PP ();

my $MAX_FILE_BYTES = 131072;
my $DEFAULT_GET_MAX_BYTES = 52428800;
my $JSON = JSON::PP->new->utf8->canonical;
my $RAW_ERRORS_TO_STDERR = 0;

sub reply_ok {
    my ($hash) = @_;
    $hash->{ok} = JSON::PP::true;
    print $JSON->encode($hash) . "\n";
    exit 0;
}

sub fail {
    my ($msg) = @_;
    $msg = "operation refused" if !defined $msg || $msg eq '';
    chomp $msg;
    if ($RAW_ERRORS_TO_STDERR) {
        print STDERR "$msg\n";
        exit 1;
    }
    print $JSON->encode({ ok => JSON::PP::false, error => "$msg" }) . "\n";
    exit 1;
}

my ($op, $compose_dir_raw, $slot_copy_raw) = @ARGV;
$RAW_ERRORS_TO_STDERR = 1 if defined $op && $op eq 'get_raw';
fail("missing operation") if !defined $op || $op eq '';

my $compose_dir = defined $compose_dir_raw ? $compose_dir_raw : '.devcontainer';
$compose_dir =~ s{^\./+}{};
$compose_dir =~ s{^/+}{};
$compose_dir =~ s{/+$}{};
$compose_dir = '.devcontainer' if $compose_dir eq '';

my @copy_files;
if (defined $slot_copy_raw && $slot_copy_raw =~ /\S/) {
    for my $entry (split /\s+/, $slot_copy_raw) {
        $entry =~ s{^\./+}{};
        $entry =~ s{^/+}{};
        $entry =~ s{/+$}{};
        push @copy_files, $entry if $entry ne '';
    }
}

my $ws_root = getcwd();
fail("cannot resolve workspace directory") if !defined $ws_root || $ws_root eq '';

binmode STDIN;
binmode STDOUT;
my $stdin_bytes = do { local $/; <STDIN> };
fail("empty stdin") if !defined $stdin_bytes || $stdin_bytes eq '';
my $req = eval { $JSON->decode($stdin_bytes) };
fail("invalid JSON object on stdin") if $@ || ref($req) ne 'HASH';

sub is_blocked_dir_rel {
    my ($rel, $name) = @_;
    return 1 if $name eq '.git';
    return 1 if $name =~ /^\.env(?:\..+)?$/;
    return 1 if $rel eq $compose_dir || index($rel, "$compose_dir/") == 0;
    for my $c (@copy_files) {
        return 1 if $rel eq $c || index($rel, "$c/") == 0;
    }
    return 0;
}

sub is_blocked_rel {
    my ($rel, $segs) = @_;
    return 1 if $rel eq 'project.env';
    return 1 if $rel eq $compose_dir || index($rel, "$compose_dir/") == 0;
    for my $c (@copy_files) {
        return 1 if $rel eq $c || index($rel, "$c/") == 0;
    }
    my $last_idx = $#$segs;
    for my $i (0 .. $last_idx) {
        my $s = $segs->[$i];
        return 1 if $s eq '.git';
        if ($s =~ /^\.env(?:\..+)?$/) {
            next if $i == $last_idx && $s eq '.env.example';
            return 1;
        }
    }
    return 0;
}

sub validate_rel_path {
    my ($p) = @_;
    fail("path must be a non-empty relative string")
        if !defined $p || ref($p) || $p eq '';
    fail("NUL byte refused in path") if index($p, "\x00") >= 0;
    fail("backslash refused in path: $p") if index($p, "\\") >= 0;
    fail("absolute path refused: $p") if substr($p, 0, 1) eq '/';

    my @segs = split m{/}, $p, -1;
    fail("empty path refused") if !@segs;
    for my $seg (@segs) {
        fail("invalid path segment in '$p'")
            if $seg eq '' || $seg eq '.' || $seg eq '..';
    }
    fail("protected path refused: $p") if is_blocked_rel($p, \@segs);
    return \@segs;
}

sub has_symlink_on_disk {
    my ($segs) = @_;
    my $cur = $ws_root;
    for my $seg (@$segs) {
        $cur .= "/" . $seg;
        my @st = lstat($cur);
        last if !@st;
        return 1 if -l _;
        last if !-d _;
    }
    return 0;
}

sub check_no_symlinks {
    my ($segs, $rel, $allow_missing_leaf) = @_;
    my $cur = $ws_root;
    my $last_idx = $#$segs;
    for my $i (0 .. $last_idx) {
        $cur .= "/" . $segs->[$i];
        my @st = lstat($cur);
        if (@st) {
            fail("symlink refused: $rel") if -l _;
            if ($i < $last_idx) {
                fail("not a directory: $rel") if !-d _;
            } else {
                fail("path is a directory: $rel") if -d _;
                fail("not a regular file: $rel") if !-f _;
            }
        } else {
            return if $allow_missing_leaf;
            fail("file not found: $rel");
        }
    }
}

sub truncate_utf8_bytes {
    my ($bytes, $max) = @_;
    return $bytes if length($bytes) <= $max;
    my $cut = substr($bytes, 0, $max);
    my $len = length($cut);
    my $cont = 0;
    while ($cont < $len && $cont < 3) {
        my $b = ord(substr($cut, $len - 1 - $cont, 1));
        last if ($b & 0xC0) != 0x80;
        $cont++;
    }
    if ($cont < $len) {
        my $lead_pos = $len - 1 - $cont;
        my $lead = ord(substr($cut, $lead_pos, 1));
        if (($lead & 0x80) == 0) {
            if ($cont > 0) {
                substr($cut, $len - $cont) = '';
            }
        } else {
            my $need = ($lead & 0xE0) == 0xC0 ? 2
                     : ($lead & 0xF0) == 0xE0 ? 3
                     : ($lead & 0xF8) == 0xF0 ? 4
                     : 0;
            if ($need == 0 || (1 + $cont) < $need) {
                substr($cut, $lead_pos) = '';
            }
        }
    } else {
        $cut = '';
    }
    return $cut;
}

sub read_regular_bytes {
    my ($rel) = @_;
    my $segs = validate_rel_path($rel);
    check_no_symlinks($segs, $rel, 0);
    my $full = "$ws_root/$rel";
    sysopen(my $fh, $full, O_RDONLY | O_NOFOLLOW)
        or fail("cannot open '$rel': $!");
    binmode $fh;
    my @st = stat($fh);
    if (!@st || !-f _) {
        close $fh;
        fail("not a regular file: $rel");
    }
    if ($st[7] > $MAX_FILE_BYTES) {
        close $fh;
        fail("file '$rel' exceeds $MAX_FILE_BYTES bytes");
    }
    my $raw = '';
    my $limit = $MAX_FILE_BYTES + 1;
    while (length($raw) < $limit) {
        my $n = sysread($fh, my $buf, $limit - length($raw));
        if (!defined $n) {
            close $fh;
            fail("read failed on '$rel': $!");
        }
        last if $n == 0;
        $raw .= $buf;
    }
    close $fh;
    fail("file '$rel' exceeds $MAX_FILE_BYTES bytes") if length($raw) > $MAX_FILE_BYTES;
    fail("binary file refused (NUL byte): $rel") if index($raw, "\x00") >= 0;
    my $raw_copy = $raw;
    my $text = eval { decode('UTF-8', $raw_copy, FB_CROAK) };
    fail("binary or invalid UTF-8 file refused: $rel") if $@ || !defined $text;
    return ($raw, $text, $st[2] & 0777);
}

sub atomic_write_bytes {
    my ($segs, $rel, $raw_out, $default_mode) = @_;
    my $full_target = "$ws_root/$rel";
    my $mode = defined $default_mode ? $default_mode : 0644;
    if (my @est = lstat($full_target)) {
        fail("symlink refused: $rel") if -l _;
        fail("path is a directory: $rel") if -d _;
        $mode = $est[2] & 0777 if -f _;
    }
    my $parent = $ws_root;
    for my $i (0 .. $#$segs - 1) {
        $parent .= "/" . $segs->[$i];
        my @pst = lstat($parent);
        if (@pst) {
            fail("symlink directory refused: $rel") if -l _;
            fail("not a directory: $rel") if !-d _;
        } else {
            mkdir($parent, 0755) or fail("mkdir failed for '$rel': $!");
            my @nst = lstat($parent);
            if (!@nst || -l _ || !-d _) {
                fail("unsafe parent directory after mkdir: $rel");
            }
        }
    }
    my $tmp = "$parent/.slotfs.tmp.$$." . int(rand(1000000000));
    sysopen(my $tfh, $tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, $mode)
        or fail("temp open failed: $!");
    binmode $tfh;
    my $written = 0;
    my $len = length($raw_out);
    while ($written < $len) {
        my $n = syswrite($tfh, $raw_out, $len - $written, $written);
        if (!defined $n) {
            close $tfh;
            unlink $tmp;
            fail("write failed: $!");
        }
        $written += $n;
    }
    if (!close($tfh)) {
        unlink $tmp;
        fail("close failed: $!");
    }
    chmod($mode, $tmp);
    if (my @tst = lstat($full_target)) {
        if (-l _ || -d _) {
            unlink $tmp;
            fail("unsafe target before rename: $rel");
        }
    }
    if (!rename($tmp, $full_target)) {
        my $err = "$!";
        unlink $tmp;
        fail("rename failed: $err");
    }
}

sub compile_glob {
    my ($pat) = @_;
    fail("glob pattern required") if !defined $pat || ref($pat) || $pat eq '';
    $pat =~ s{^\./+}{};
    my $re = '^';
    my $i = 0;
    my $len = length($pat);
    while ($i < $len) {
        if (substr($pat, $i, 3) eq '**/') {
            $re .= '(?:.+/)?';
            $i += 3;
        } elsif (substr($pat, $i, 2) eq '**') {
            $re .= '.*';
            $i += 2;
        } else {
            my $ch = substr($pat, $i, 1);
            if ($ch eq '*') {
                $re .= '[^/]*';
            } elsif ($ch eq '?') {
                $re .= '[^/]';
            } else {
                $re .= quotemeta($ch);
            }
            $i += 1;
        }
    }
    $re .= '$';
    my $compiled = eval { qr/$re/ };
    fail("invalid glob pattern") if $@ || !$compiled;
    return $compiled;
}

sub walk_workspace_files {
    my ($glob_qr, $start_prefix) = @_;
    $start_prefix = '' if !defined $start_prefix;
    my @out;
    my @queue = ($start_prefix);
    my $sub_offset = $start_prefix eq '' ? 0 : length($start_prefix) + 1;
    while (@queue) {
        my $prefix = shift @queue;
        my $dir_full = $prefix eq '' ? $ws_root : "$ws_root/$prefix";
        opendir(my $dh, $dir_full) or next;
        my @entries = sort readdir($dh);
        closedir $dh;
        for my $entry (@entries) {
            next if $entry eq '.' || $entry eq '..';
            my $rel = $prefix eq '' ? $entry : "$prefix/$entry";
            my $full = "$ws_root/$rel";
            my @st = lstat($full);
            next if !@st || -l _;
            if (-d _) {
                next if is_blocked_dir_rel($rel, $entry);
                push @queue, $rel;
            } elsif (-f _) {
                my @segs = split m{/}, $rel, -1;
                next if is_blocked_rel($rel, \@segs);
                if (!defined $glob_qr
                    || $rel =~ $glob_qr
                    || ($sub_offset > 0 && substr($rel, $sub_offset) =~ $glob_qr)) {
                    push @out, $rel;
                }
            }
        }
    }
    @out = sort @out;
    return \@out;
}

sub resolve_grep_files {
    my ($glob_qr) = @_;
    if (!defined $req->{path} || $req->{path} eq '') {
        return walk_workspace_files($glob_qr, '');
    }
    my $scope = $req->{path};
    fail("path must be a string") if ref($scope);
    fail("NUL byte refused in path") if index($scope, "\x00") >= 0;
    fail("backslash refused in path: $scope") if index($scope, "\\") >= 0;
    fail("absolute path refused: $scope") if substr($scope, 0, 1) eq '/';
    return walk_workspace_files($glob_qr, '') if $scope eq '.';

    $scope =~ s{/$}{};
    my @segs = split m{/}, $scope, -1;
    fail("empty path refused") if !@segs;
    for my $seg (@segs) {
        fail("invalid path segment in '$scope'")
            if $seg eq '' || $seg eq '.' || $seg eq '..';
    }
    fail("protected path refused: $scope") if is_blocked_rel($scope, \@segs);

    my $cur = $ws_root;
    my $last_idx = $#segs;
    my $is_dir = 0;
    for my $i (0 .. $last_idx) {
        $cur .= "/" . $segs[$i];
        my @st = lstat($cur);
        fail("path not found: $scope") if !@st;
        fail("symlink refused: $scope") if -l _;
        if ($i < $last_idx) {
            fail("not a directory: $scope") if !-d _;
        } else {
            if (-d _) {
                fail("protected path refused: $scope")
                    if is_blocked_dir_rel($scope, $segs[$i]);
                $is_dir = 1;
            } elsif (!-f _) {
                fail("not a regular file or directory: $scope");
            }
        }
    }

    if ($is_dir) {
        return walk_workspace_files($glob_qr, $scope);
    }
    if (!defined $glob_qr || $scope =~ $glob_qr || $segs[-1] =~ $glob_qr) {
        return [$scope];
    }
    return [];
}

sub op_read {
    my ($raw, $text) = read_regular_bytes($req->{path});
    my $sha = sha256_hex($raw);
    my @lines;
    if (length($text) > 0) {
        my @parts = split /\n/, $text, -1;
        if (@parts && $parts[-1] eq '') {
            pop @parts;
            @lines = map { "$_\n" } @parts;
        } else {
            my $last = pop @parts;
            @lines = (map({ "$_\n" } @parts), $last);
        }
    }
    my $total = scalar(@lines);
    my $out_content = $text;
    if (defined $req->{offset_line} || defined $req->{limit_lines}) {
        my $off = defined $req->{offset_line} ? int($req->{offset_line}) : 1;
        fail("offset_line must be >= 1") if $off < 1;
        my $start = $off - 1;
        if ($start >= $total) {
            $out_content = '';
        } else {
            my $end = $total - 1;
            if (defined $req->{limit_lines}) {
                my $lim = int($req->{limit_lines});
                fail("limit_lines must be >= 0") if $lim < 0;
                if ($lim == 0) {
                    $end = -1;
                } elsif ($start + $lim - 1 < $end) {
                    $end = $start + $lim - 1;
                }
            }
            $out_content = $end >= $start ? join('', @lines[$start .. $end]) : '';
        }
    }
    reply_ok({
        content     => $out_content,
        total_lines => $total,
        sha256      => $sha,
    });
}

sub op_glob {
    my $qr = compile_glob($req->{pattern});
    fail("invalid max_results") if defined $req->{max_results} && ref($req->{max_results});
    my $max_results = defined $req->{max_results} ? int($req->{max_results}) : 1000;
    fail("max_results must be >= 1") if $max_results < 1;

    my $paths = walk_workspace_files($qr);
    my $truncated = 0;
    if (scalar(@$paths) > $max_results) {
        @$paths = @$paths[0 .. $max_results - 1];
        $truncated = 1;
    }
    reply_ok({
        paths     => $paths,
        truncated => ($truncated ? JSON::PP::true : JSON::PP::false),
    });
}

sub op_grep {
    my $pat = $req->{pattern};
    fail("non-empty pattern required") if !defined $pat || ref($pat) || $pat eq '';
    my $fixed = ($req->{fixed_strings} || $req->{is_literal}) ? 1 : 0;
    my $rx = $fixed
        ? qr/\Q$pat\E/
        : eval { qr/$pat/ };
    fail("invalid grep pattern") if !$rx;

    my $glob_qr;
    if (defined $req->{glob}) {
        fail("glob must be a string") if ref($req->{glob});
        if ($req->{glob} ne '') {
            $glob_qr = compile_glob($req->{glob});
        }
    }
    fail("invalid max_results") if defined $req->{max_results} && ref($req->{max_results});
    fail("invalid max_matches") if defined $req->{max_matches} && ref($req->{max_matches});
    my $max_matches = defined $req->{max_results}
        ? int($req->{max_results})
        : (defined $req->{max_matches} ? int($req->{max_matches}) : 200);
    fail("max_results must be >= 1") if $max_matches < 1;

    my $files = resolve_grep_files($glob_qr);
    my @matches;
    my $truncated = 0;

    FILE_LOOP:
    for my $rel (@$files) {
        my $full = "$ws_root/$rel";
        sysopen(my $fh, $full, O_RDONLY | O_NOFOLLOW) or next;
        binmode $fh;
        my @st = stat($fh);
        if (!@st || !-f _ || $st[7] > $MAX_FILE_BYTES) {
            close $fh;
            next;
        }
        my $raw = '';
        my $limit = $MAX_FILE_BYTES + 1;
        my $read_err = 0;
        while (length($raw) < $limit) {
            my $n = sysread($fh, my $buf, $limit - length($raw));
            if (!defined $n) { $read_err = 1; last; }
            last if $n == 0;
            $raw .= $buf;
        }
        close $fh;
        next if $read_err || length($raw) > $MAX_FILE_BYTES || index($raw, "\x00") >= 0;
        my $raw_copy = $raw;
        my $text = eval { decode('UTF-8', $raw_copy, FB_CROAK) };
        next if $@ || !defined $text;

        my @lines = split /\n/, $text, -1;
        pop @lines if @lines && $lines[-1] eq '';
        my $line_no = 0;
        for my $line (@lines) {
            $line_no++;
            $line =~ s/\r$//;
            if ($line =~ $rx) {
                if (@matches >= $max_matches) {
                    $truncated = 1;
                    last FILE_LOOP;
                }
                push @matches, {
                    path => $rel,
                    line => $line_no,
                    text => $line,
                };
            }
        }
    }

    reply_ok({
        matches   => \@matches,
        truncated => ($truncated ? JSON::PP::true : JSON::PP::false),
    });
}

sub op_diff {
    fail("cached must be a boolean")
        if defined $req->{cached} && (ref($req->{cached}) eq 'ARRAY' || ref($req->{cached}) eq 'HASH');
    my @cached_flag = $req->{cached} ? ('--cached') : ();

    fail("invalid max_bytes") if defined $req->{max_bytes} && ref($req->{max_bytes});
    my $max_bytes = defined $req->{max_bytes} ? int($req->{max_bytes}) : $MAX_FILE_BYTES;
    fail("max_bytes must be >= 1") if $max_bytes < 1;

    my @paths;
    if (defined $req->{paths}) {
        fail("paths must be an array") if ref($req->{paths}) ne 'ARRAY';
        for my $raw_p (@{ $req->{paths} }) {
            fail("path must be a non-empty relative string")
                if !defined $raw_p || ref($raw_p) || $raw_p eq '';
            my $p = $raw_p;
            $p =~ s{/$}{} if $p ne '/';
            my $segs = validate_rel_path($p);
            fail("symlink refused: $p") if has_symlink_on_disk($segs);
            if (my @lst = lstat("$ws_root/$p")) {
                if (-d _ && is_blocked_dir_rel($p, $segs->[-1])) {
                    fail("protected path refused: $p");
                }
            }
            push @paths, $p;
        }
    }

    open(
        my $nfh, '-|',
        'git', '--literal-pathspecs', '-C', $ws_root, 'diff', '--no-ext-diff', '--no-renames',
        @cached_flag, '--name-only', '-z', '--', (@paths ? @paths : ('.'))
    ) or fail("git diff failed: $!");
    binmode $nfh;
    my $names_raw = do { local $/; <$nfh> };
    if (!close($nfh) && $? != 0) {
        fail("git diff failed");
    }
    $names_raw = '' if !defined $names_raw;

    my @raw_names = split /\x00/, $names_raw, -1;
    pop @raw_names if @raw_names && $raw_names[-1] eq '';

    my %seen_allowed;
    my @allowed_files;
    NAME_LOOP:
    for my $rel (@raw_names) {
        next if $rel eq '' || substr($rel, 0, 1) eq '/' || index($rel, "\\") >= 0;
        my @segs = split m{/}, $rel, -1;
        next if !@segs;
        for my $seg (@segs) {
            next NAME_LOOP if $seg eq '' || $seg eq '.' || $seg eq '..';
        }
        next if is_blocked_rel($rel, \@segs);
        next if has_symlink_on_disk(\@segs);
        if (!$seen_allowed{$rel}++) {
            push @allowed_files, $rel;
        }
    }

    if (!@allowed_files) {
        reply_ok({
            diff      => '',
            truncated => JSON::PP::false,
        });
    }

    open(
        my $gfh, '-|',
        'git', '--literal-pathspecs', '-C', $ws_root, 'diff', '--no-ext-diff', '--no-color',
        @cached_flag, '--', @allowed_files
    ) or fail("git diff failed: $!");
    binmode $gfh;
    my $raw_diff = '';
    my $limit = $max_bytes + 1;
    while (length($raw_diff) < $limit) {
        my $n = sysread($gfh, my $buf, $limit - length($raw_diff));
        if (!defined $n) {
            close $gfh;
            fail("read failed on git diff: $!");
        }
        last if $n == 0;
        $raw_diff .= $buf;
    }
    my $truncated = 0;
    if (length($raw_diff) > $max_bytes) {
        close $gfh;
        $truncated = 1;
        $raw_diff = truncate_utf8_bytes($raw_diff, $max_bytes);
    } else {
        if (!close($gfh) && $? != 0) {
            fail("git diff failed");
        }
    }

    my $diff_text = decode('UTF-8', $raw_diff);
    reply_ok({
        diff      => $diff_text,
        truncated => ($truncated ? JSON::PP::true : JSON::PP::false),
    });
}

sub op_write {
    my $rel = $req->{path};
    my $segs = validate_rel_path($rel);
    my $content = $req->{content};
    fail("content string required") if !defined $content || ref($content);
    fail("NUL byte refused in content") if index($content, "\x00") >= 0;
    my $raw_out = encode('UTF-8', $content);
    fail("content exceeds $MAX_FILE_BYTES bytes") if length($raw_out) > $MAX_FILE_BYTES;

    check_no_symlinks($segs, $rel, 1);
    if (defined $req->{expected_sha256} && !ref($req->{expected_sha256}) && $req->{expected_sha256} ne '') {
        my ($cur_raw) = read_regular_bytes($rel);
        fail("CAS sha256 mismatch") if sha256_hex($cur_raw) ne lc($req->{expected_sha256});
    }

    atomic_write_bytes($segs, $rel, $raw_out, 0644);
    reply_ok({
        sha256 => sha256_hex($raw_out),
        bytes  => length($raw_out),
    });
}

sub op_edit {
    my $rel = $req->{path};
    my $segs = validate_rel_path($rel);
    my ($cur_raw, $cur_text, $mode) = read_regular_bytes($rel);
    if (defined $req->{expected_sha256} && !ref($req->{expected_sha256}) && $req->{expected_sha256} ne '') {
        fail("CAS sha256 mismatch") if sha256_hex($cur_raw) ne lc($req->{expected_sha256});
    }
    my $old = $req->{old_string};
    my $new = $req->{new_string};
    fail("non-empty old_string required") if !defined $old || ref($old) || $old eq '';
    fail("new_string required") if !defined $new || ref($new);
    fail("NUL byte refused in old_string") if index($old, "\x00") >= 0;
    fail("NUL byte refused in new_string") if index($new, "\x00") >= 0;

    my @pieces;
    my $count = 0;
    my $pos = 0;
    my $old_len = length($old);
    while ((my $idx = index($cur_text, $old, $pos)) >= 0) {
        $count++;
        push @pieces, substr($cur_text, $pos, $idx - $pos), $new;
        $pos = $idx + $old_len;
    }
    fail("old_string not found in '$rel'") if $count == 0;
    my $replace_all = $req->{replace_all} ? 1 : 0;
    if ($count > 1 && !$replace_all) {
        fail("old_string matches $count times in '$rel'; set replace_all or provide more context");
    }
    push @pieces, substr($cur_text, $pos);
    my $updated_text = join('', @pieces);
    my $raw_out = encode('UTF-8', $updated_text);
    fail("edited content exceeds $MAX_FILE_BYTES bytes") if length($raw_out) > $MAX_FILE_BYTES;

    atomic_write_bytes($segs, $rel, $raw_out, $mode);
    reply_ok({
        replacements => $count,
        sha256       => sha256_hex($raw_out),
    });
}

sub op_get_raw {
    my $max_bytes = $DEFAULT_GET_MAX_BYTES;
    if (defined $req->{max_bytes}) {
        fail("max_bytes must be a positive integer")
            if ref($req->{max_bytes}) || $req->{max_bytes} !~ /^[1-9][0-9]*$/;
        $max_bytes = int($req->{max_bytes});
        fail("max_bytes must be >= 1") if $max_bytes < 1;
    }
    my $rel = $req->{path};
    my $segs = validate_rel_path($rel);
    check_no_symlinks($segs, $rel, 0);
    my $full = "$ws_root/$rel";
    sysopen(my $fh, $full, O_RDONLY | O_NOFOLLOW)
        or fail("cannot open '$rel': $!");
    binmode $fh;
    my @st = stat($fh);
    if (!@st || !-f _) {
        close $fh;
        fail("not a regular file: $rel");
    }
    if ($st[7] > $max_bytes) {
        close $fh;
        fail("file '$rel' exceeds $max_bytes bytes");
    }
    my $total = 0;
    while (1) {
        my $n = sysread($fh, my $buf, 65536);
        if (!defined $n) {
            close $fh;
            fail("read failed on '$rel': $!");
        }
        last if $n == 0;
        if ($total + $n > $max_bytes) {
            close $fh;
            fail("file '$rel' exceeds $max_bytes bytes");
        }
        $total += $n;
        my $off = 0;
        while ($off < $n) {
            my $w = syswrite(STDOUT, $buf, $n - $off, $off);
            if (!defined $w) {
                close $fh;
                fail("write failed for '$rel': $!");
            }
            $off += $w;
        }
    }
    close $fh;
    exit 0;
}

if    ($op eq 'read')    { op_read(); }
elsif ($op eq 'glob')    { op_glob(); }
elsif ($op eq 'grep')    { op_grep(); }
elsif ($op eq 'diff')    { op_diff(); }
elsif ($op eq 'write')   { op_write(); }
elsif ($op eq 'edit')    { op_edit(); }
elsif ($op eq 'get_raw') { op_get_raw(); }
else                     { fail("unsupported operation: $op"); }

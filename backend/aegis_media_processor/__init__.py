"""The credential-free, network-free media processor (M2).

The processor decodes hostile media in a sandbox. The container has no network, no database
access and no secrets, a read-only root filesystem, no capabilities and ``no-new-privileges``.
Inside it:

- the loop (``loop.py``) never decodes and never imports Pillow. It opens each original itself,
  by validated components and with no symlink followed, and checks the fingerprint on the open
  descriptor (``source.py``);
- each job runs in one decoding child (``child.py``) that inherits only that descriptor. Before
  ``exec``, the child gets rlimits (address space, CPU, file size, open files, no core), nice 10,
  a scrubbed environment, ``PR_SET_PDEATHSIG``, ``no_new_privs`` and a Landlock ruleset
  (``landlock.py``): read and execute only for the system libraries and the Python installation,
  read for ``/etc``, ``/dev/urandom`` and its own ``job.json``, read and write for ``/dev/null``,
  writes only beneath the job's fresh ``out/`` directory, no other root or scratch path, no TCP
  (ABI 4) or UDP (ABI 10), no pathname Unix sockets outside itself (ABI 9), and no abstract
  sockets or signals outside itself (ABI 6). Then a seccomp filter (``seccomp.py``) denies what
  Landlock cannot: chmod, chown, utime and xattr changes, ptrace and process memory access, and
  namespace and mount calls. ffmpeg and ffprobe inherit all of it and read the descriptor through
  the ``fd:`` protocol;
- a deadline kills the child's process group. The loop is a child subreaper and kills and reaps
  every stray descendant after each job, so nothing a job started outlives it. Each pass restores
  the scratch directories' modes;
- results are strict, bounded documents (``protocol.py``). The loop accepts only declared
  outputs that are plain files of the declared size and format, and the worker must treat the
  published result as untrusted all the same.

Without Landlock ABI 3 or newer (no kernel support, or a container seccomp profile that blocks
it), or off x86-64 (no filter), the processor refuses to start; there is no unconfined fallback.

Remaining channels, accepted: Landlock does not mediate POSIX message queues or SysV IPC, so
concurrent children could signal each other through them (they hold nothing to leak but their
own job's media); and a child can fill ``out/`` with many files, each bounded by
``RLIMIT_FSIZE``, so the scratch volume's size bound (Task 11) is the final limit.
"""

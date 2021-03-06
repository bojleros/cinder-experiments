
### Problem definition

There are some workloads that require performant low-latency storage. Usually nowadays this means nvme but not always. Since our performance and volume requirements are hardly met by NVMe (with reasonable price) we decided to deploy directly attached storage (local storage) based on SATA ssd and hardware RAID controller.

What we are using now is direct nova lvm integration but it has some limitations:
- numerous flavors with rootdisk
- unable to scale compute/ram and storage independently
- unable to separate data volumes from operating system volume therefore no way of using storage with different performance/cost profile for a given job
- resize means reboot

What we wanted to do is to use Cinder but we learned that lvm volume gets attached via iscsi. This resulted in rather big performance penalty during the PoC so we were somehow forced to stick with nova lvm.

### Performance impact estimation

#### Methodology

Benchmark of following devices was performed in order to find the culprit:
1. pv of the Cinder volume group
2. test lv created manually in the Cinder volume group
3. regular lv created by Cinder
4. sd* device created by iscsi initiator (volume was attached to vm but vm was shut down leaving sd* unlocked for tests)

Benchmarks was executed on 48core/128GB dell with two ssd's attached into a hardware RAID mirror. VD was configured with no read-ahead, write-trough with caches disabled. Benchmark conducted on CentOS 7 host running stable/stein compute node. We decided to run with libaio and O_DIRECT since it is a default setting for most of databases:

```
#write benchmark
fio --name=randwrite --rw=randwrite --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
#read benchmark
fio --name=randread --rw=randread --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
```

#### Results

|Test case|Read kiops|Write kiops|Comment|
|--|--|--|--|
|1| 100 | 30 |
|2| 90 | 31 |
|3| 92 | 32 | Baseline lvm
|4|36.7|27.5| Iscsi


Details are shown on the end of this paper. What we can see here is a performance drop introduced by LIO iscsi. I did not bothered with testing tgtadm since it's results was even worse with occasional hiccups and lockouts. It is worthy of noting that overall cpu consumption of  all target threads was almost 1 cpu core in both cases. I believe there are a lot of people who will not be bothered by this fact but for us it is a bit too much.

### Improvement proposal

It looks like os-brick and Cinder have already anything we need. On the Cinder side there is this nice lvm driver that provides all functionalities we need to manage volume. On the other side there is a support for LOCAL initiator and connector in os-brick. That put's me into a conclusion that creation of a separate driver would be a code duplication so the only thing that we need is a new target helper (based on the one from iscsi).

```
diff --git a/cinder/volume/driver.py b/cinder/volume/driver.py
index 6c9c474..4b024c3 100644
--- a/cinder/volume/driver.py
+++ b/cinder/volume/driver.py
@@ -82,12 +82,13 @@ volume_opts = [
     cfg.StrOpt('target_helper',
                default='tgtadm',
                choices=['tgtadm', 'lioadm', 'scstadmin', 'iscsictl',
-                        'ietadm', 'nvmet', 'spdk-nvmeof', 'fake'],
+                        'ietadm', 'nvmet', 'spdk-nvmeof', 'direct', 'fake'],
                help='Target user-land tool to use. tgtadm is default, '
                     'use lioadm for LIO iSCSI support, scstadmin for SCST '
                     'target support, ietadm for iSCSI Enterprise Target, '
                     'iscsictl for Chelsio iSCSI Target, nvmet for NVMEoF '
                     'support, spdk-nvmeof for SPDK NVMe-oF, '
+                    'direct for direct lvm attachment,'
                     'or fake for testing.'),
     cfg.StrOpt('volumes_dir',
                default='$state_path/volumes',
@@ -427,7 +428,9 @@ class BaseVD(object):
             'scstadmin': 'cinder.volume.targets.scst.SCSTAdm',
             'iscsictl': 'cinder.volume.targets.cxt.CxtAdm',
             'nvmet': 'cinder.volume.targets.nvmet.NVMET',
-            'spdk-nvmeof': 'cinder.volume.targets.spdknvmf.SpdkNvmf'}
+            'spdk-nvmeof': 'cinder.volume.targets.spdknvmf.SpdkNvmf',
+            'direct': 'cinder.volume.targets.direct.DirectTarget'
+        }
 
         # set True by manager after successful check_for_setup
         self._initialized = False
@@ -2514,6 +2517,7 @@ class ProxyVD(object):
         __getattr__) without directly inheriting from base volume driver this
         class can help marking them and retrieve the actual used driver object.
     """
+
     def _get_driver(self):
         """Returns the actual driver object.
 
@@ -2794,6 +2798,7 @@ class ISERDriver(ISCSIDriver):
       '<auth method> <auth username> <auth password>'.
       `CHAP` is the only auth_method in use at the moment.
     """
+
     def __init__(self, *args, **kwargs):
         super(ISERDriver, self).__init__(*args, **kwargs)
         # for backward compatibility
@@ -2849,6 +2854,7 @@ class ISERDriver(ISCSIDriver):
 
 class FibreChannelDriver(VolumeDriver):
     """Executes commands relating to Fibre Channel volumes."""
+
     def __init__(self, *args, **kwargs):
         super(FibreChannelDriver, self).__init__(*args, **kwargs)
 
diff --git a/cinder/volume/targets/direct.py b/cinder/volume/targets/direct.py
new file mode 100644
index 0000000..f07666f
--- /dev/null
+++ b/cinder/volume/targets/direct.py
@@ -0,0 +1,102 @@
+#    Licensed under the Apache License, Version 2.0 (the "License"); you may
+#    not use this file except in compliance with the License. You may obtain
+#    a copy of the License at
+#
+#         http://www.apache.org/licenses/LICENSE-2.0
+#
+#    Unless required by applicable law or agreed to in writing, software
+#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
+#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
+#    License for the specific language governing permissions and limitations
+#    under the License.
+
+import abc
+
+from oslo_concurrency import processutils
+from oslo_log import log as logging
+
+from cinder import exception
+from cinder.i18n import _
+from cinder import utils
+from cinder.volume.targets import driver
+from cinder.volume import volume_utils
+
+LOG = logging.getLogger(__name__)
+
+
+class DirectTarget(driver.Target):
+    """Target object for block storage devices.
+
+    Base class for target object, where target
+    is data transport mechanism (target) specific calls.
+    This includes things like create targets, attach, detach
+    etc.
+    """
+
+    def __init__(self, *args, **kwargs):
+        super(DirectTarget, self).__init__(*args, **kwargs)
+        self.protocol = 'local'
+        self.volumes_dir = self.configuration.safe_get('volumes_dir')
+
+    def create_export(self, context, volume, volume_path):
+        """Creates an export for a logical volume."""
+        LOG.debug("Create export on volume %s in path %s" %
+                  (volume, volume_path))
+        data = {'device_path': volume_path, 'auth': None,
+                'location': volume.availability_zone}
+
+        return data
+
+    def remove_export(self, context, volume):
+        LOG.debug("Unexport on volume %s" % volume)
+
+    def ensure_export(self, context, volume, volume_path):
+        """Recreates an export for a logical volume."""
+        LOG.debug("Ensure export on volume %s in path %s" %
+                  (volume, volume_path))
+
+    def initialize_connection(self, volume, connector):
+        """Initializes the connection and returns connection info.
+        """
+
+        return {
+            'driver_volume_type': self.protocol,
+            'data': {'device_path': "/dev/%s/volume-%s" % (self.configuration.safe_get('volume_group'), volume.id)}
+        }
+
+    def terminate_connection(self, volume, connector, **kwargs):
+        pass
+
+    def validate_connector(self, connector):
+        # NOTE(jdg): api passes in connector which is initiator info
+        if 'initiator' not in connector:
+            err_msg = ('The volume driver requires the local initiator '
+                       'name in the connector.')
+            LOG.error(err_msg)
+            raise exception.InvalidConnectorException(missing='initiator')
+        return True
+
+    def extend_target(self, volume):
+        """Reinitializes a target after the LV has been extended.
+
+        Note: This will cause IO disruption in most cases.
+        """
+        LOG.debug("Extend target on volume %s" %
+                  (volume))
+
```

Tested functionalities include (in fact inherited from current functions of lvm driver):
- Create/Destroy
- Resize/Retype
- Attach/Detach
- Snapshot
- Create from Image, Convert into Image

Despite this fact you must still remember that volumes from this backend are going to be available on a single hosts so:
- you must ensure that vm is pinned or running in a single-host-AZ (you may also want to limit Cinder to a particular AZ) or without it your vm can be started on a host that does not contain your volumes
- you are aware that data can be lost due to server failure
- you can mitigate risk of data lost caused by drive failure by using RAID
- you should run backups or have other ways of restoring the data
- directly it will make vm migration impossible
- directly it will not allow of a simple volume data migration

Tricks to consider :
- it may be possible to migrate volume to another host via shared storage by making a double volume retype
- it may be possible to allow vm migration if guest os uses lvm and there is a shared storage available, the problem is how we do the pinning

Benefits:
- new performant option available for existing lvm driver
- freedom of choice
- should be compatible with existing deployments
- minimal change footprint
- it should be possible to switch between lio/tgtadm and direct while vm is shut down (and one ensures vm-storage placement rules fulfilled)

### Detailed benchmark data
```
1. Determine baseline performance of tested device

Remark: I do not have a spare disks so i have to reuse remaining space on a root disks. This is carved out by megaraid controller and lvm:

/mnt # lsblk 
NAME                      MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda                         8:0    0    2G  0 disk 
├─sda1                      8:1    0  200M  0 part /boot/efi
└─sda2                      8:2    0  1.8G  0 part /boot
sdb                         8:16   0   38G  0 disk 
├─vgsystem-lvroot         253:0    0    8G  0 lvm  /
└─vgsystem-lvlog          253:1    0    2G  0 lvm  /var/log
sdc                         8:32   0  183G  0 disk 
├─vgapp-lv_var_lib_docker 253:2    0   50G  0 lvm  /var/lib/docker
└─vgapp-test              253:3    0  100G  0 lvm  /mnt

We are going to create nested lvm on vgapp-test in the next steps. VD performance settings are coherent all across our deployments and we use the same ssd family.

/mnt # fio --name=randwrite --rw=randwrite --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
randwrite: (g=0): rw=randwrite, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
...
fio-3.1
Starting 16 processes
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
Jobs: 10 (f=10): [_(1),w(6),_(1),w(2),_(1),w(1),_(2),w(1),_(1)][98.5%][r=0KiB/s,w=201MiB/s][r=0,w=51.5k IOPS][eta 00m:02s]
randwrite: (groupid=0, jobs=16): err= 0: pid=205978: Mon Apr 27 15:30:25 2020
  write: IOPS=31.9k, BW=125MiB/s (131MB/s)(16.0GiB/131274msec)
    slat (usec): min=7, max=24628, avg=29.41, stdev=288.20
    clat (nsec): min=925, max=25077k, avg=467688.37, stdev=451029.21
     lat (usec): min=37, max=25200, avg=497.27, stdev=535.75
    clat percentiles (usec):
     |  1.00th=[  128],  5.00th=[  194], 10.00th=[  219], 20.00th=[  269],
     | 30.00th=[  314], 40.00th=[  359], 50.00th=[  429], 60.00th=[  490],
     | 70.00th=[  553], 80.00th=[  619], 90.00th=[  701], 95.00th=[  766],
     | 99.00th=[ 1336], 99.50th=[ 1532], 99.90th=[ 2040], 99.95th=[ 3458],
     | 99.99th=[23200]
   bw (  KiB/s): min= 5896, max=22556, per=6.25%, avg=7992.68, stdev=1132.17, samples=4175
   iops        : min= 1474, max= 5639, avg=1997.98, stdev=283.06, samples=4175
  lat (nsec)   : 1000=0.01%
  lat (usec)   : 2=0.01%, 4=0.01%, 10=0.01%, 20=0.01%, 50=0.08%
  lat (usec)   : 100=0.40%, 250=16.74%, 500=44.61%, 750=32.65%, 1000=1.92%
  lat (msec)   : 2=3.50%, 4=0.06%, 10=0.01%, 20=0.01%, 50=0.03%
  cpu          : usr=0.82%, sys=6.00%, ctx=4260293, majf=0, minf=2118
  IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
     submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     issued rwt: total=0,4194304,0, short=0,0,0, dropped=0,0,0
     latency   : target=0, window=0, percentile=100.00%, depth=1

Run status group 0 (all jobs):
  WRITE: bw=125MiB/s (131MB/s), 125MiB/s-125MiB/s (131MB/s-131MB/s), io=16.0GiB (17.2GB), run=131274-131274msec

Disk stats (read/write):
    dm-3: ios=0/5231320, merge=0/0, ticks=0/2980275, in_queue=2993004, util=100.00%, aggrios=0/4245483, aggrmerge=0/993390, aggrticks=0/1982355, aggrin_queue=1983354, aggrutil=99.63%
  sdc: ios=0/4245483, merge=0/993390, ticks=0/1982355, in_queue=1983354, util=99.63%


/mnt # fio --name=randread --rw=randread --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
  randread: (g=0): rw=randread, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
  ...
  fio-3.1
  Starting 16 processes
  Jobs: 13 (f=10): [r(1),_(3),r(5),f(1),r(2),f(1),r(2),f(1)][61.8%][r=6023MiB/s,w=0KiB/s][r=1542k,w=0 IOPS][eta 00m:13s]
  randread: (groupid=0, jobs=16): err= 0: pid=207504: Mon Apr 27 15:32:11 2020
     read: IOPS=199k, BW=776MiB/s (813MB/s)(16.0GiB/21120msec)
      slat (nsec): min=1325, max=2267.3k, avg=6140.35, stdev=5933.29
      clat (nsec): min=584, max=3983.2k, avg=73215.56, stdev=76803.69
       lat (nsec): min=1974, max=3993.3k, avg=79408.24, stdev=81113.92
      clat percentiles (nsec):
       |  1.00th=[   596],  5.00th=[   628], 10.00th=[   636], 20.00th=[   644],
       | 30.00th=[   652], 40.00th=[   660], 50.00th=[   788], 60.00th=[127488],
       | 70.00th=[140288], 80.00th=[150528], 90.00th=[166912], 95.00th=[181248],
       | 99.00th=[209920], 99.50th=[220160], 99.90th=[246784], 99.95th=[259072],
       | 99.99th=[350208]
     bw (  KiB/s): min=23928, max=900140, per=3.89%, avg=30922.49, stdev=63759.69, samples=659
     iops        : min= 5982, max=225035, avg=7730.58, stdev=15939.91, samples=659
    lat (nsec)   : 750=49.64%, 1000=1.35%
    lat (usec)   : 2=0.09%, 4=0.01%, 10=0.03%, 20=0.01%, 50=0.06%
    lat (usec)   : 100=0.34%, 250=48.39%, 500=0.07%, 750=0.01%, 1000=0.01%
    lat (msec)   : 2=0.01%, 4=0.01%
    cpu          : usr=2.84%, sys=9.90%, ctx=2050709, majf=0, minf=1083
    IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
       submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       issued rwt: total=4194304,0,0, short=0,0,0, dropped=0,0,0
       latency   : target=0, window=0, percentile=100.00%, depth=1

  Run status group 0 (all jobs):
     READ: bw=776MiB/s (813MB/s), 776MiB/s-776MiB/s (813MB/s-813MB/s), io=16.0GiB (17.2GB), run=21120-21120msec

  Disk stats (read/write):
      dm-3: ios=2050017/196, merge=0/0, ticks=304938/170, in_queue=307600, util=98.38%, aggrios=2050017/190, aggrmerge=0/6, aggrticks=305936/169, aggrin_queue=306566, aggrutil=97.76%
    sdc: ios=2050017/190, merge=0/6, ticks=305936/169, in_queue=306566, util=97.76%

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
So it is ~100kiops rd and ~30kiops wr. I do not know why we get RD result blurred. --direct should bypass os cache and therefore i was forced to use iostat on the other terminal.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!



2. We create nested vg and mount test volume:

/mnt # lsblk 
NAME                      MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda                         8:0    0    2G  0 disk 
├─sda1                      8:1    0  200M  0 part /boot/efi
└─sda2                      8:2    0  1.8G  0 part /boot
sdb                         8:16   0   38G  0 disk 
├─vgsystem-lvroot         253:0    0    8G  0 lvm  /
└─vgsystem-lvlog          253:1    0    2G  0 lvm  /var/log
sdc                         8:32   0  183G  0 disk 
├─vgapp-lv_var_lib_docker 253:2    0   50G  0 lvm  /var/lib/docker
└─vgapp-test              253:3    0  100G  0 lvm  
  └─test-test             253:4    0   30G  0 lvm  /mnt

  /mnt # fio --name=randwrite --rw=randwrite --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
  randwrite: (g=0): rw=randwrite, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
  ...
  fio-3.1
  Starting 16 processes
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  randwrite: Laying out IO file (1 file / 1024MiB)
  Jobs: 6 (f=6): [_(5),w(1),_(2),w(1),_(1),w(1),_(1),w(2),_(1),w(1)][99.2%][r=0KiB/s,w=189MiB/s][r=0,w=48.5k IOPS][eta 00m:01s]
  randwrite: (groupid=0, jobs=16): err= 0: pid=209911: Mon Apr 27 15:40:00 2020
    write: IOPS=31.0k, BW=125MiB/s (131MB/s)(16.0GiB/131218msec)
      slat (usec): min=8, max=33705, avg=30.16, stdev=306.32
      clat (nsec): min=895, max=34128k, avg=466356.03, stdev=509195.05
       lat (usec): min=38, max=34154, avg=496.67, stdev=594.67
      clat percentiles (usec):
       |  1.00th=[  118],  5.00th=[  190], 10.00th=[  217], 20.00th=[  265],
       | 30.00th=[  310], 40.00th=[  355], 50.00th=[  429], 60.00th=[  486],
       | 70.00th=[  545], 80.00th=[  611], 90.00th=[  693], 95.00th=[  758],
       | 99.00th=[ 1336], 99.50th=[ 1532], 99.90th=[ 2114], 99.95th=[ 3621],
       | 99.99th=[26346]
     bw (  KiB/s): min= 6384, max=27584, per=6.27%, avg=8010.82, stdev=1273.60, samples=4174
     iops        : min= 1596, max= 6896, avg=2002.57, stdev=318.42, samples=4174
    lat (nsec)   : 1000=0.01%
    lat (usec)   : 2=0.01%, 4=0.01%, 10=0.01%, 20=0.01%, 50=0.07%
    lat (usec)   : 100=0.67%, 250=16.87%, 500=44.62%, 750=32.42%, 1000=1.77%
    lat (msec)   : 2=3.47%, 4=0.06%, 10=0.01%, 20=0.01%, 50=0.03%
    cpu          : usr=0.80%, sys=6.19%, ctx=4257817, majf=0, minf=2135
    IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
       submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       issued rwt: total=0,4194304,0, short=0,0,0, dropped=0,0,0
       latency   : target=0, window=0, percentile=100.00%, depth=1

  Run status group 0 (all jobs):
    WRITE: bw=125MiB/s (131MB/s), 125MiB/s-125MiB/s (131MB/s-131MB/s), io=16.0GiB (17.2GB), run=131218-131218msec

  Disk stats (read/write):
      dm-4: ios=0/5210381, merge=0/0, ticks=0/2447303, in_queue=2454180, util=99.88%, aggrios=0/5215813, aggrmerge=0/0, aggrticks=0/2441483, aggrin_queue=2454451, aggrutil=100.00%
      dm-3: ios=0/5215813, merge=0/0, ticks=0/2441483, in_queue=2454451, util=100.00%, aggrios=0/4245356, aggrmerge=0/970457, aggrticks=0/1968163, aggrin_queue=1969067, aggrutil=99.67%
    sdc: ios=0/4245356, merge=0/970457, ticks=0/1968163, in_queue=1969067, util=99.67%

    /mnt # fio --name=randread --rw=randread --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
    randread: (g=0): rw=randread, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
    ...
    fio-3.1
    Starting 16 processes
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    randread: Laying out IO file (1 file / 1024MiB)
    Jobs: 5 (f=4): [_(3),r(4),_(4),f(1),_(4)][100.0%][r=293MiB/s,w=0KiB/s][r=75.0k,w=0 IOPS][eta 00m:00s]
    randread: (groupid=0, jobs=16): err= 0: pid=212043: Mon Apr 27 15:44:08 2020
       read: IOPS=90.9k, BW=355MiB/s (372MB/s)(16.0GiB/46122msec)
        slat (usec): min=3, max=2247, avg=10.89, stdev= 5.99
        clat (nsec): min=805, max=9791.3k, avg=156856.32, stdev=272138.87
         lat (usec): min=27, max=9810, avg=167.81, stdev=272.45
        clat percentiles (usec):
         |  1.00th=[   38],  5.00th=[   96], 10.00th=[  113], 20.00th=[  125],
         | 30.00th=[  133], 40.00th=[  141], 50.00th=[  147], 60.00th=[  153],
         | 70.00th=[  161], 80.00th=[  172], 90.00th=[  186], 95.00th=[  200],
         | 99.00th=[  231], 99.50th=[  247], 99.90th=[ 7242], 99.95th=[ 7373],
         | 99.99th=[ 7570]
       bw (  KiB/s): min=  537, max=92393, per=5.58%, avg=20295.77, stdev=7601.78, samples=1401
       iops        : min=  134, max=23098, avg=5073.58, stdev=1900.44, samples=1401
      lat (nsec)   : 1000=0.01%
      lat (usec)   : 2=0.01%, 10=0.01%, 20=0.01%, 50=1.38%, 100=4.39%
      lat (usec)   : 250=93.79%, 500=0.28%, 750=0.01%, 1000=0.01%
      lat (msec)   : 2=0.01%, 4=0.01%, 10=0.14%
      cpu          : usr=2.10%, sys=8.81%, ctx=4195181, majf=0, minf=1897
      IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
         submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
         complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
         issued rwt: total=4194304,0,0, short=0,0,0, dropped=0,0,0
         latency   : target=0, window=0, percentile=100.00%, depth=1

    Run status group 0 (all jobs):
       READ: bw=355MiB/s (372MB/s), 355MiB/s-355MiB/s (372MB/s-372MB/s), io=16.0GiB (17.2GB), run=46122-46122msec

    Disk stats (read/write):
        dm-4: ios=4190485/31, merge=0/0, ticks=659112/0, in_queue=664312, util=100.00%, aggrios=4194304/31, aggrmerge=0/0, aggrticks=655209/0, aggrin_queue=659266, aggrutil=100.00%
        dm-3: ios=4194304/31, merge=0/0, ticks=655209/0, in_queue=659266, util=100.00%, aggrios=4194304/14, aggrmerge=0/23, aggrticks=658155/0, aggrin_queue=658717, aggrutil=99.97%
      sdc: ios=4194304/14, merge=0/23, ticks=658155/0, in_queue=658717, util=99.97%


This time we have a good results on randread. We get rd 90kiops and 31kiops wr. We lost approx 10k on rd because of nesting. This is a new ceiling.


3. Benchmark volume device directly:

/mnt # lsblk 
NAME                                                      MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda                                                         8:0    0    2G  0 disk 
├─sda1                                                      8:1    0  200M  0 part /boot/efi
└─sda2                                                      8:2    0  1.8G  0 part /boot
sdb                                                         8:16   0   38G  0 disk 
├─vgsystem-lvroot                                         253:0    0    8G  0 lvm  /
└─vgsystem-lvlog                                          253:1    0    2G  0 lvm  /var/log
sdc                                                         8:32   0  183G  0 disk 
├─vgapp-lv_var_lib_docker                                 253:2    0   50G  0 lvm  /var/lib/docker
└─vgapp-test                                              253:3    0  100G  0 lvm  
  └─test-volume--103d6ef2--cbf9--4d3a--bd0f--a9cec220698a 253:4    0   60G  0 lvm  /mnt

/mnt # fio --name=randwrite --rw=randwrite --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
randwrite: (g=0): rw=randwrite, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
...
fio-3.1
Starting 16 processes
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
Jobs: 11 (f=11): [w(1),_(1),w(5),_(2),w(5),_(2)][98.5%][r=0KiB/s,w=206MiB/s][r=0,w=52.6k IOPS][eta 00m:02s]
randwrite: (groupid=0, jobs=16): err= 0: pid=263595: Mon Apr 27 17:16:47 2020
  write: IOPS=31.9k, BW=125MiB/s (131MB/s)(16.0GiB/131434msec)
    slat (usec): min=8, max=28474, avg=31.03, stdev=326.73
    clat (nsec): min=906, max=28657k, avg=467100.63, stdev=495311.65
     lat (usec): min=38, max=29163, avg=498.29, stdev=593.98
    clat percentiles (usec):
     |  1.00th=[  129],  5.00th=[  192], 10.00th=[  217], 20.00th=[  269],
     | 30.00th=[  310], 40.00th=[  359], 50.00th=[  429], 60.00th=[  490],
     | 70.00th=[  545], 80.00th=[  611], 90.00th=[  693], 95.00th=[  766],
     | 99.00th=[ 1336], 99.50th=[ 1532], 99.90th=[ 2147], 99.95th=[ 3556],
     | 99.99th=[26346]
   bw (  KiB/s): min= 5987, max=18138, per=6.25%, avg=7976.25, stdev=1066.71, samples=4182
   iops        : min= 1496, max= 4534, avg=1993.75, stdev=266.69, samples=4182
  lat (nsec)   : 1000=0.01%
  lat (usec)   : 2=0.01%, 4=0.01%, 10=0.01%, 20=0.01%, 50=0.03%
  lat (usec)   : 100=0.41%, 250=17.03%, 500=44.55%, 750=32.54%, 1000=1.79%
  lat (msec)   : 2=3.55%, 4=0.07%, 10=0.01%, 20=0.01%, 50=0.03%
  cpu          : usr=0.82%, sys=6.22%, ctx=4262439, majf=0, minf=2294
  IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
     submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     issued rwt: total=0,4194304,0, short=0,0,0, dropped=0,0,0
     latency   : target=0, window=0, percentile=100.00%, depth=1

Run status group 0 (all jobs):
  WRITE: bw=125MiB/s (131MB/s), 125MiB/s-125MiB/s (131MB/s-131MB/s), io=16.0GiB (17.2GB), run=131434-131434msec

Disk stats (read/write):
    dm-4: ios=0/5223319, merge=0/0, ticks=0/2474929, in_queue=2485996, util=100.00%, aggrios=0/5228322, aggrmerge=0/0, aggrticks=0/2468943, aggrin_queue=2482084, aggrutil=100.00%
    dm-3: ios=0/5228322, merge=0/0, ticks=0/2468943, in_queue=2482084, util=100.00%, aggrios=0/4245735, aggrmerge=0/982587, aggrticks=0/1971548, aggrin_queue=1972075, aggrutil=99.70%
  sdc: ios=0/4245735, merge=0/982587, ticks=0/1971548, in_queue=1972075, util=99.70%

  /mnt # fio --name=randread --rw=randread --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
  randread: (g=0): rw=randread, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
  ...
  fio-3.1
  Starting 16 processes
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  Jobs: 13 (f=13): [r(13),_(3)][97.9%][r=351MiB/s,w=0KiB/s][r=89.0k,w=0 IOPS][eta 00m:01s]
  randread: (groupid=0, jobs=16): err= 0: pid=265055: Mon Apr 27 17:18:59 2020
     read: IOPS=91.7k, BW=358MiB/s (376MB/s)(16.0GiB/45717msec)
      slat (usec): min=3, max=3491, avg=10.67, stdev= 5.96
      clat (nsec): min=837, max=9725.1k, avg=154695.09, stdev=271374.15
       lat (usec): min=27, max=9745, avg=165.43, stdev=271.59
      clat percentiles (usec):
       |  1.00th=[   38],  5.00th=[   92], 10.00th=[  110], 20.00th=[  123],
       | 30.00th=[  133], 40.00th=[  139], 50.00th=[  145], 60.00th=[  151],
       | 70.00th=[  159], 80.00th=[  169], 90.00th=[  184], 95.00th=[  196],
       | 99.00th=[  225], 99.50th=[  239], 99.90th=[ 7177], 99.95th=[ 7373],
       | 99.99th=[ 7570]
     bw (  KiB/s): min=  540, max=93072, per=6.01%, avg=22059.79, stdev=7894.80, samples=1378
     iops        : min=  135, max=23268, avg=5514.61, stdev=1973.70, samples=1378
    lat (nsec)   : 1000=0.01%
    lat (usec)   : 2=0.01%, 4=0.01%, 10=0.01%, 20=0.01%, 50=1.41%
    lat (usec)   : 100=5.33%, 250=92.92%, 500=0.18%, 750=0.01%, 1000=0.01%
    lat (msec)   : 2=0.01%, 4=0.01%, 10=0.14%
    cpu          : usr=2.09%, sys=8.76%, ctx=4195558, majf=0, minf=1912
    IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
       submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       issued rwt: total=4194304,0,0, short=0,0,0, dropped=0,0,0
       latency   : target=0, window=0, percentile=100.00%, depth=1

  Run status group 0 (all jobs):
     READ: bw=358MiB/s (376MB/s), 358MiB/s-358MiB/s (376MB/s-376MB/s), io=16.0GiB (17.2GB), run=45717-45717msec

  Disk stats (read/write):
      dm-4: ios=4189332/42, merge=0/0, ticks=649030/0, in_queue=656687, util=100.00%, aggrios=4194304/42, aggrmerge=0/0, aggrticks=645102/0, aggrin_queue=649560, aggrutil=100.00%
      dm-3: ios=4194304/42, merge=0/0, ticks=645102/0, in_queue=649560, util=100.00%, aggrios=4194304/13, aggrmerge=0/29, aggrticks=648646/0, aggrin_queue=648940, aggrutil=99.95%
    sdc: ios=4194304/13, merge=0/29, ticks=648646/0, in_queue=648940, util=99.95%

We get similar results as in step 2. That's expected.

4. Benchmark device exposed by iscsi initiator:

/mnt # lsscsi 
[0:2:0:0]    disk    DELL     PERC H730P Mini  4.30  /dev/sda 
[0:2:1:0]    disk    DELL     PERC H730P Mini  4.30  /dev/sdb 
[0:2:2:0]    disk    DELL     PERC H730P Mini  4.30  /dev/sdc 
[15:0:0:0]   disk    LIO-ORG  block1           4.0   /dev/sdd 

/mnt # lsblk 
NAME                                                      MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda                                                         8:0    0    2G  0 disk 
├─sda1                                                      8:1    0  200M  0 part /boot/efi
└─sda2                                                      8:2    0  1.8G  0 part /boot
sdb                                                         8:16   0   38G  0 disk 
├─vgsystem-lvroot                                         253:0    0    8G  0 lvm  /
└─vgsystem-lvlog                                          253:1    0    2G  0 lvm  /var/log
sdc                                                         8:32   0  183G  0 disk 
├─vgapp-lv_var_lib_docker                                 253:2    0   50G  0 lvm  /var/lib/docker
└─vgapp-test                                              253:3    0  100G  0 lvm  
  └─test-volume--103d6ef2--cbf9--4d3a--bd0f--a9cec220698a 253:4    0   60G  0 lvm  
sdd                                                         8:48   0   60G  0 disk /mnt

/mnt # fio --name=randwrite --rw=randwrite --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
randwrite: (g=0): rw=randwrite, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
...
fio-3.1
Starting 16 processes
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
randwrite: Laying out IO file (1 file / 1024MiB)
Jobs: 6 (f=6): [_(7),w(1),_(1),w(3),_(1),w(1),_(1),w(1)][100.0%][r=0KiB/s,w=115MiB/s][r=0,w=29.5k IOPS][eta 00m:00s]
randwrite: (groupid=0, jobs=16): err= 0: pid=258373: Mon Apr 27 17:04:30 2020
  write: IOPS=27.4k, BW=107MiB/s (112MB/s)(16.0GiB/153010msec)
    slat (usec): min=6, max=40089, avg=18.80, stdev=263.77
    clat (usec): min=26, max=61844, avg=561.01, stdev=715.89
     lat (usec): min=67, max=61859, avg=579.94, stdev=763.30
    clat percentiles (usec):
     |  1.00th=[  233],  5.00th=[  293], 10.00th=[  334], 20.00th=[  388],
     | 30.00th=[  424], 40.00th=[  453], 50.00th=[  498], 60.00th=[  578],
     | 70.00th=[  635], 80.00th=[  701], 90.00th=[  766], 95.00th=[  832],
     | 99.00th=[ 1467], 99.50th=[ 1713], 99.90th=[ 2008], 99.95th=[ 4621],
     | 99.99th=[36439]
   bw (  KiB/s): min= 5672, max=21776, per=6.27%, avg=6872.37, stdev=683.68, samples=4865
   iops        : min= 1418, max= 5444, avg=1718.00, stdev=170.90, samples=4865
  lat (usec)   : 50=0.01%, 100=0.04%, 250=1.64%, 500=48.77%, 750=37.59%
  lat (usec)   : 1000=9.68%
  lat (msec)   : 2=2.18%, 4=0.05%, 10=0.01%, 20=0.01%, 50=0.04%
  lat (msec)   : 100=0.01%
  cpu          : usr=0.55%, sys=3.51%, ctx=4277960, majf=0, minf=1611
  IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
     submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
     issued rwt: total=0,4194304,0, short=0,0,0, dropped=0,0,0
     latency   : target=0, window=0, percentile=100.00%, depth=1

Run status group 0 (all jobs):
  WRITE: bw=107MiB/s (112MB/s), 107MiB/s-107MiB/s (112MB/s-112MB/s), io=16.0GiB (17.2GB), run=153010-153010msec

Disk stats (read/write):
  sdd: ios=0/4262600, merge=0/972714, ticks=0/2500390, in_queue=2503863, util=99.81%

  /mnt # fio --name=randread --rw=randread --direct=1 --ioengine=libaio --bs=4k --numjobs=16 --size=1G --runtime=600 --group_reporting
  randread: (g=0): rw=randread, bs=(R) 4096B-4096B, (W) 4096B-4096B, (T) 4096B-4096B, ioengine=libaio, iodepth=1
  ...
  fio-3.1
  Starting 16 processes
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  randread: Laying out IO file (1 file / 1024MiB)
  Jobs: 14 (f=14): [r(14),_(2)][100.0%][r=151MiB/s,w=0KiB/s][r=38.8k,w=0 IOPS][eta 00m:00s]
  randread: (groupid=0, jobs=16): err= 0: pid=260434: Mon Apr 27 17:08:33 2020
     read: IOPS=36.7k, BW=143MiB/s (150MB/s)(16.0GiB/114211msec)
      slat (usec): min=3, max=392, avg= 6.52, stdev= 1.52
      clat (usec): min=12, max=9468, avg=421.15, stdev=265.74
       lat (usec): min=60, max=9483, avg=427.74, stdev=265.94
      clat percentiles (usec):
       |  1.00th=[  273],  5.00th=[  338], 10.00th=[  355], 20.00th=[  375],
       | 30.00th=[  388], 40.00th=[  400], 50.00th=[  412], 60.00th=[  424],
       | 70.00th=[  437], 80.00th=[  453], 90.00th=[  474], 95.00th=[  490],
       | 99.00th=[  523], 99.50th=[  537], 99.90th=[ 7177], 99.95th=[ 7373],
       | 99.99th=[ 7570]
     bw (  KiB/s): min=  537, max=48961, per=5.96%, avg=8755.28, stdev=2203.56, samples=3576
     iops        : min=  134, max=12240, avg=2188.51, stdev=550.89, samples=3576
    lat (usec)   : 20=0.01%, 100=0.75%, 250=0.18%, 500=95.97%, 750=2.92%
    lat (usec)   : 1000=0.01%
    lat (msec)   : 2=0.01%, 4=0.01%, 10=0.14%
    cpu          : usr=0.61%, sys=2.21%, ctx=4194761, majf=0, minf=1334
    IO depths    : 1=100.0%, 2=0.0%, 4=0.0%, 8=0.0%, 16=0.0%, 32=0.0%, >=64=0.0%
       submit    : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       complete  : 0=0.0%, 4=100.0%, 8=0.0%, 16=0.0%, 32=0.0%, 64=0.0%, >=64=0.0%
       issued rwt: total=4194304,0,0, short=0,0,0, dropped=0,0,0
       latency   : target=0, window=0, percentile=100.00%, depth=1

  Run status group 0 (all jobs):
     READ: bw=143MiB/s (150MB/s), 143MiB/s-143MiB/s (150MB/s-150MB/s), io=16.0GiB (17.2GB), run=114211-114211msec

  Disk stats (read/write):
    sdd: ios=4193945/12, merge=0/29, ticks=1764171/0, in_queue=1764025, util=100.00%

So we have 27.4kiops wr and 36.7kiops on rd.

5. Conclusion

Steps 1-3 was performed in order to determine overall performance impact introduced by nested lvm.
Due to lack of separate disks i was forced to execute tests on system disks but RAID setup is exactly the same.
Disks are also made by same manufacturer and comes from the same product series.
Comparing results from (3) and (4) we see a lost of ~4kiops on write operations and lost of 53.3kiops on rd.
In this case we loose about 60% of our read performance.
```

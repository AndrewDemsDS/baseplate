# Ignore policy for the weekly image scan (.github/workflows/scan-images.yml).
#
# linux-libc-dev ships kernel headers only. Containers run on the host kernel,
# so a kernel CVE reported against an image is a property of the NAS host, not
# of the image. Tdarr alone contributed 11 criticals of this kind on the first
# scheduled run.
#
# Matching on package name rather than CVE ID on purpose: kernel CVEs land
# weekly, so an enumerated list would go stale. Every other package still fails
# the scan normally.

package trivy

default ignore = false

ignore {
	input.PkgName == "linux-libc-dev"
}

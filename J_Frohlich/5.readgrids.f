      program readgrids
cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
c
c     purpose: read grid files for periodic hill configuration
c
c                                            j. froehlich 14.3.2005
cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc

c-------------------------------------------------------------------
c     NOTE: the numbers given in [Mellen,Fröhlich,Rodi, 2000]
c           are different because dummy cells used for periodic
c           boundary conditions were counted also.
c-------------------------------------------------------------------

      character*50 string

c-----wall-resolving grid
      parameter (ni=197,nj=129)
      real*8 xcorner(ni,nj)
      real*8 ycorner(ni,nj)
      character*31 filename

      filename='wall-resolving_grid_MFR2000.dat'


c-----wall-function grid
c     parameter (ni=177,nj=65)
c     real*8 xcorner(ni,nj)
c     real*8 ycorner(ni,nj)
c     character*30 filename

c     filename='wall-function_grid_MFR2000.dat'


c-----read grid
      write(*,*) 'open file ',filename
      open(1,file=filename,status='OLD',form='FORMATTED',err=1001)

c     ----- read header
      do i=1,6
        read(1,*) string
        write(*,*) 'read : ', string
      enddo

c     ----- read points
      do j=1,nj
        do i=1,ni
          read(1,*,err=1002) xcorner(i,j), ycorner(i,j) 
        enddo
      enddo

      close(1)

c-----add your own processing here


c-----regular termination
      stop 'terminated normally'

c-----errors
1001  stop 'error upon opening file'
1002  stop 'error upon reading'
      end

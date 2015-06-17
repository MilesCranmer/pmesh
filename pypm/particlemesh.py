"""
PyPM: A Particle Mesh code in Python

"""
import pfft
import domain
import cic
import numpy
import time
from mpi4py import MPI
from tools import Timers

class ParticleMesh(object):
    """
    ParticleMesh provides an interface to solver for forces
    with particle mesh method

    ParticleMesh object is a state machine. Refer to :ref:`introduction` on 
    the standard steps to use this object.

    Memory usage is twice the size of the FFT mesh. However, 
    Be aware the transfer functions may take more memory.

    Attributes
    ----------
    np      : array_like (npx, npy)
        The shape of the process mesh. This is the number of domains per direction.
        The product of the items shall equal to the size of communicator. 
        For example, for 64 rank job, np = (8, 8) is a good choice.
        Since for now only 3d simulations are supported, np must be of length-2.
        The default is try to split the total number of ranks equally. (eg, for
        a 64 rank job, default is (8, 8)

    comm    : :py:class:`MPI.Comm`
        the MPI communicator, (default is MPI.COMM_WORLD)

    Nmesh   : int
        number of mesh points per side. Only 3d simulations are supported by now,
        and the true mesh is [Nmesh, Nmesh, Nmesh]

    BoxSize : float
        size of box
    
    domain   : :py:class:`pypm.domain.GridND`
        domain decomposition (private)

    partition : :py:class:`pfft.Partition`
        domain partition (private)

    real   : array_like
        the real FFT array (private)

    complex : array_like
        the complex FFT array (private)

    T    : :py:class:`pypm.tools.Timers`
        profiling timers

    """
    def __init__(self, BoxSize, Nmesh, comm=None, np=None, verbose=False, dtype='f8'):
        """ create a PM object.  """
        # this weird sequence to intialize comm is because
        # we want to be compatible with None comm == MPI.COMM_WORLD
        # while not relying on pfft's full mpi4py compatibility
        # (passing None through to pfft)
        if comm is None:
            self.comm = MPI.COMM_WORLD
        else:
            self.comm = comm
        if np is None:
                np = pfft.split_size_2d(self.comm.size)

        dtype = numpy.dtype(dtype)
        if dtype is numpy.dtype('f8'):
            forward = pfft.Type.PFFT_R2C
            backward = pfft.Type.PFFT_C2R
        elif dtype is numpy.dtype('f4'):
            forward = pfft.Type.PFFTF_R2C
            backward = pfft.Type.PFFTF_C2R
        else:
            raise ValueError("dtype must be f8 or f4")

        self.procmesh = pfft.ProcMesh(np, comm=comm)
        self.Nmesh = Nmesh
        self.BoxSize = BoxSize
        self.partition = pfft.Partition(forward,
            [Nmesh, Nmesh, Nmesh], 
            self.procmesh,
            pfft.Flags.PFFT_TRANSPOSED_OUT | pfft.Flags.PFFT_DESTROY_INPUT)

        buffer = pfft.LocalBuffer(self.partition)
        self.real = buffer.view_input()
        self.real[:] = 0

        self.complex = buffer.view_output()

        self.T = Timers(self.comm)
        with self.T['Plan']:
            self.forward = pfft.Plan(self.partition, pfft.Direction.PFFT_FORWARD,
                    self.real.base, self.complex.base, forward,
                    pfft.Flags.PFFT_ESTIMATE | pfft.Flags.PFFT_TRANSPOSED_OUT | pfft.Flags.PFFT_DESTROY_INPUT)
            self.backward = pfft.Plan(self.partition, pfft.Direction.PFFT_BACKWARD,
                    self.complex.base, self.real.base, backward, 
                    pfft.Flags.PFFT_ESTIMATE | pfft.Flags.PFFT_TRANSPOSED_IN | pfft.Flags.PFFT_DESTROY_INPUT)

        self.domain = domain.GridND(self.partition.i_edges)
        self.verbose = verbose
        self.stack = []

    def transform(self, x):
        """ 
        Transform from simulation unit to local grid unit.

        Parameters
        ----------
        x     : array_like (, Ndim)
            coordinates in simulation unit

        Returns
        -------
        ret   : array_like (, Ndim)
            coordinates in local grid unit
 
        """
        ret = (1.0 * self.Nmesh / self.BoxSize) * x - self.partition.local_i_start
        #print self.partition.local_i_start, (ret.max(axis=0), ret.min(axis=0))
        return ret

    def transform0(self, x):
        """ 
        Transform from simulation unit to global grid unit.

        Parameters
        ----------
        x     : array_like (, Ndim)
            coordinates in simulation unit

        Returns
        -------
        ret   : array_like (, Ndim)
            coordinates in global grid unit
 
        """
        ret = (1.0 * self.Nmesh / self.BoxSize) * x
        #print self.partition.local_i_start, (ret.max(axis=0), ret.min(axis=0))
        return ret

    def decompose(self, pos):
        """ 
        Create a domain decompose layout for particles at given
        coordinates.

        Parameters
        ----------
        pos    : array_like (, Ndim)
            position of particles in simulation  unit

        Returns
        -------
        layout  : :py:class:domain.Layout
            layout that can be used to migrate particles and images
        to the correct MPI ranks that hosts the PM local mesh
        """
        with self.T['Decompose']:
            return self.domain.decompose(pos, smoothing=1.0,
                    transform=self.transform0)
    def clear(self):
        """ 
        Clear the internal real canvas. 

        This function simply set real[:] = 0. After :py:meth:`clear`, 
        :py:meth:`paint` can correctly paint to the canvas.

        Notes
        -----
        A freshly created :py:class:`ParticleMesh` object come with
        a cleared canvas.
    
        """

    def paint(self, pos, mass=1.0):
        """ 
        Paint particles into the internal real canvas. 

        Transform the particle field given by pos and mass
        to the overdensity field in fourier space and save
        it in the internal storage. 
        A multi-linear CIC approximation scheme is used.

        The function can be called multiple times: 
        the result is cummulative. In a multi-step simulation where
        :py:class:`ParticleMesh` object is reused,  before calling 
        :py:meth:`paint`, make sure the canvas is cleared with :py:meth:`clear`.

        Parameters
        ----------
        pos    : array_like (, Ndim)
            position of particles in simulation  unit

        mass   : scalar or array_like (,)
            mass of particles in simulation  unit


        Notes
        -----
        self.real is NOT the density field after this operation.
        It has the dimension of mass but is divided by :code:`Nmesh**3`.
        Density is :code:`real * Nmesh ** 3 / BoxSize ** 3`

        :py:meth:`r2c` will convert to a density unit by dividing
        by :code:`BoxSize**3`.

        PFFT will adjust for this Nmesh**3 after r2c .
    
        """
        with self.T['Paint']:
            cic.paint(pos, self.real, weights=mass, mode='ignore',
                    period=self.Nmesh, transform=self.transform)

    def r2c(self, pos=None, mass=1.0):
        """ 
        Perform real to complex FFT on the internal canvas.

        If pos and mass are given, :py:meth:`paint` is called to
        paint the particles before running the fourier transform.

        Parameters
        ----------
        pos    : array_like (, Ndim)
            position of particles in simulation  unit

        mass   : scalar or array_like (,)
            mass of particles in simulation  unit

        """
        if pos is not None:
            self.clear()
            self.paint(pos, mass)

        self.real *= (1.0 / self.BoxSize) ** 3
        if self.verbose:
            realsum = self.comm.allreduce(self.real.sum(dtype='f8'), MPI.SUM)
            if self.comm.rank == 0:
                print 'before r2c, sum of real', realsum
            self.comm.barrier()

        with self.T['R2C']:
            self.forward.execute(self.real.base, self.complex.base)

        if self.procmesh.rank == 0:
            # remove the mean !
            # self.complex.flat[0] = 0
            pass

    def push(self):
        """ 
        Save the complex field 
        
        The complex field is saved to an internal stack. Recover the
        complex field with :py:meth:`pop`.

        """
        self.stack.append(self.complex.copy())

    def pop(self):
        """ 
        Restore the complex field 
        
        The complex field was saved to an internal stack by :py:meth:`push`. 

        """
        self.complex[:] = self.stack.pop()

    def transfer(self, transfer_functions):
        """ 
        Apply transfer functions

        Apply a chain of transfer functions to the complex field, in place.
        If the original field shall be preserved, use :py:meth:`push`.
    
        Parameters
        ----------
        transfer_functions : list of :py:class:transfer.TransferFunction 
            A chain of transfer functions to apply to the complex field. 
        
        """
        w = []
        for d in range(self.partition.Ndim):
            s = numpy.ones(self.partition.Ndim, dtype='intp')
            s[d] = self.partition.local_no[d]
            wi = numpy.arange(s[d], dtype='f4') + self.partition.local_o_start[d] 
            wi[wi >= self.Nmesh // 2] -= self.Nmesh
            wi *= (2 * numpy.pi / self.Nmesh)
            w.append(wi.reshape(s))

        with self.T['Transfer']:
            for transfer in transfer_functions:
                transfer(self.comm, self.complex, w)

    def readout(self, pos):
        """ 
        Read out from real field at positions
            
        Parameters
        ----------
        pos    : array_like (, Ndim)
            position of particles in simulation  unit
        
        Returns
        -------
        rt     : array_like (,)
            read out values from the real field.
 
        """
        with self.T['Readout']:
            if pos is not None:
                rt = cic.readout(self.real, pos, mode='ignore', period=self.Nmesh,
                        transform=self.transform)
                return rt
        
    def c2r(self, transfer_functions):
        """ 
        Complex to real transformation.
        
        Parameters
        ----------
        transfer_functions  : list of :py:class:`transfer.TransferFunction`
            A chain of transfer functions to apply to the complex field. 

        """
        self.push()

        self.transfer(transfer_functions)

        with self.T['C2R']:
            self.backward.execute(self.complex.base, self.real.base)
        if self.verbose:
            realsum = self.comm.allreduce(self.real.sum(dtype='f8'), MPI.SUM)
            if self.comm.rank == 0:
                print 'after c2r, sum of real', realsum
            self.comm.barrier()

        # restore the complex field, for next c2r transform
        self.pop()

